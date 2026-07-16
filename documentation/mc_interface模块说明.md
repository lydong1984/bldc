# mc_interface 模块说明

本文说明 `bldc/motor/mc_interface.c` 与 `mc_interface.h` 的设计职责、内部状态、公开接口和运行机制。该模块是上层应用与具体电机控制算法之间的统一边界，同时承担安全准入、故障处理、动态限幅、测量统计及双电机上下文管理。

## 1. 模块定位

`mc_interface` 位于应用/通信层和电机控制后端之间。上层通常不直接调用 `mcpwm` 或 `mcpwm_foc`，而是通过 `mc_interface_*()` 使用统一语义的控制与查询接口。

```mermaid
flowchart TB
    APP["应用层<br/>ADC、PPM、UART、LispBM、自定义应用"]
    COMM["通信层<br/>CAN、USB、commands、terminal"]
    IF["mc_interface.c/.h<br/>统一接口与系统协调"]
    CONF["mc_configuration<br/>控制参数与安全限制"]
    BLDC["mcpwm<br/>BLDC 六步换相 / DC"]
    FOC["mcpwm_foc<br/>FOC"]
    ENC["encoder<br/>位置传感器"]
    DRV["栅极驱动与 hwconf"]
    BMS["BMS、事件、关机与统计模块"]
    HW["ADC / TIM / DMA / 功率级 / 电机"]

    APP --> IF
    COMM --> IF
    CONF <--> IF
    IF -->|BLDC 或 DC| BLDC
    IF -->|FOC| FOC
    IF <--> ENC
    IF <--> DRV
    IF <--> BMS
    BLDC <--> HW
    FOC <--> HW
```

它的核心价值是让上层不依赖具体控制算法，并把所有控制命令先纳入同一套锁定、忽略窗口、方向和安全检查。

## 2. 文件分工

### 2.1 mc_interface.h

头文件定义模块的公开边界：

- 初始化、配置与双电机选择接口；
- 占空比、电流、制动、速度、位置和开环控制接口；
- 电流、电压、转速、能量、温度和里程查询接口；
- 故障、调试采样、统计和输入屏蔽接口；
- 供具体电机后端调用的公共 ISR/故障接口；
- `ADC_Value`、`ADC_curr_norm_value`、`ADC_curr_raw` 三组共享采样数组；
- `HW_DEAD_TIME_NSEC` 的通用默认值。

### 2.2 mc_interface.c

实现文件负责：

- 保存每台电机的接口层状态；
- 初始化配置、线程、编码器、栅极驱动和控制后端；
- 将统一 API 路由到 `mcpwm` 或 `mcpwm_foc`；
- 在实时中断中进行公共采样、计量、故障和限幅处理；
- 在线程中执行慢速监控、故障停机、波形发送和统计；
- 处理双电机选择及单线程的目标电机上下文。

## 3. 内部状态模型

每台电机对应一个私有 `motor_if_state_t`：

```mermaid
classDiagram
    class motor_if_state_t {
        mc_configuration m_conf
        mc_fault_code m_fault_now
        setup_stats m_stats
        int m_ignore_iterations
        int m_drv_fault_iterations
        bool m_lock_enabled
        bool m_lock_override_once
        float m_motor_current_sum
        float m_input_current_sum
        float m_amp_seconds
        float m_watt_seconds
        float m_position_set
        float m_temp_fet
        float m_temp_motor
        float m_input_voltage_filtered
        float m_motor_current_unbalance
        uint64_t m_odometer_last
        uint64_t m_runtime_last
    }

    class mc_configuration {
        motor_type
        control_parameters
        current_voltage_limits
        temperature_limits
        sensor_configuration
        bms_configuration
    }

    class setup_stats {
        averages
        maximums
        sample_count
    }

    motor_if_state_t *-- mc_configuration
    motor_if_state_t *-- setup_stats
```

主要状态可分为五类：

| 类别 | 代表成员 | 用途 |
|---|---|---|
| 配置与故障 | `m_conf`、`m_fault_now` | 当前配置和统一故障状态 |
| 命令准入 | `m_ignore_iterations`、`m_lock_enabled`、`m_lock_override_once` | 控制命令屏蔽与一次性放行 |
| 测量累计 | 电流、电压、`Id/Iq`、`Vd/Vq` 累加器 | 提供平均测量和调试结果 |
| 能量与统计 | Ah、Wh、里程、运行时间、`m_stats` | 遥测和长期统计 |
| 慢速监控 | 温度、驱动故障计数、电流不平衡、滤波电压 | 动态限幅及故障判定 |

单电机硬件只存在 `m_motor_1`；定义 `HW_HAS_DUAL_MOTORS` 时还存在 `m_motor_2`。

## 4. 初始化流程

`mc_interface_init(reset_conf)` 建立接口层及具体电机后端：

```mermaid
flowchart TD
    A[进入 mc_interface_init] --> B[清零 motor_if_state_t]
    B --> C{reset_conf?}
    C -->|是| D[生成默认 mc_configuration]
    D --> E[写入持久化存储]
    C -->|否| F[从持久化存储读取配置]
    E --> G[初始化调试采样与统计状态]
    F --> G
    G --> H[创建 4 个后台线程]
    H --> I[按配置设置栅极驱动器]
    I --> J[encoder_init]
    J --> K{motor_type}
    K -->|BLDC / DC| L[mcpwm_init]
    K -->|FOC| M[mcpwm_foc_init]
    L --> N[bms_init]
    M --> N
```

双电机硬件会读取两份配置，并分别设置两路驱动器。当前代码在 `HW_HAS_DUAL_MOTORS` 下强制两台电机使用 FOC，以满足共享定时/采样资源的实现约束。

初始化会创建：

| 线程名 | 优先级 | 职责 |
|---|---:|---|
| `mcif timer` | `NORMALPRIO` | 每 1 ms 运行慢速监控、动态限幅和故障检查 |
| `SampleSender` | `NORMALPRIO - 1` | 将中断采样缓冲区打包并通过回调发送 |
| `Fault Stop` | `HIGHPRIO - 3` | 将 ISR/线程发现的故障转化为完整停机处理 |
| 统计线程 | `NORMALPRIO` | 每 10 ms 更新功率、速度、电流和温度统计 |

## 5. 公开 API 分类

### 5.1 生命周期与配置

| API | 作用 |
|---|---|
| `mc_interface_init()` | 初始化接口层、配置、后台线程和控制后端 |
| `mc_interface_get_configuration()` | 返回当前所选电机配置的只读易失指针 |
| `mc_interface_set_configuration()` | 应用配置，必要时重启控制后端和编码器 |
| `mc_interface_calc_crc()` | 计算配置 CRC |
| `mc_interface_dccal_done()` | 查询当前控制后端的直流偏置校准状态 |
| `mc_interface_set_pwm_callback()` | 注册每个 PWM 周期结束时从中断调用的回调 |

### 5.2 控制命令

| 控制类别 | API |
|---|---|
| 占空比 | `set_duty()`、`set_duty_noramp()` |
| 速度/位置 | `set_pid_speed()`、`set_pid_pos()` |
| 电流/制动 | `set_current()`、`set_brake_current()`、相对值版本 |
| 手刹 | `set_handbrake()`、`set_handbrake_rel()` |
| FOC 开环 | `set_openloop_current/phase/duty()` 及组合版本 |
| 停止与释放 | `brake_now()`、`release_motor()`、`release_motor_override()` |
| 其他 | 设置转速计值、当前关闭延时、位置偏移 |

控制接口执行的是“设置控制目标”，并不等待电机达到目标。实际 PWM 更新由后端的 ADC/PWM 中断完成。

### 5.3 状态与测量

查询 API 覆盖：

- 运行状态、控制模式、故障码；
- 当前/目标占空比、采样频率和 RPM；
- 电机电流、输入电流及滤波/方向版本；
- Ah、Wh、充电累计值；
- FET/电机温度与输入电压；
- 转速计、速度、距离、里程和电池余量；
- `Id/Iq`、`Vd/Vq` 和 ADC ISR 执行时间等调试量；
- `setup_values` 聚合状态和 GNSS 数据。

### 5.4 安全、统计和内部协作

| API 组 | 作用 |
|---|---|
| `lock/unlock/lock_override_once` | 全局屏蔽当前电机控制命令或一次性放行 |
| `ignore_input*` | 在指定毫秒数内拒绝控制输入 |
| `fault_stop()` | 从 ISR 或线程侧提交统一故障 |
| `mc_timer_isr()` | 供具体控制后端在实时周期调用公共逻辑 |
| `adc_inj_int_handler()` | 将注入 ADC 中断路由给 BLDC/DC 后端 |
| `sample_print_data()` | 配置 ADC/PWM 调试采样 |
| `stat_*()` | 查询或清零运行统计 |

## 6. 控制命令的准入与分派

所有主要控制命令首先调用 `mc_interface_try_input()`：

```mermaid
flowchart TD
    A[上层提交控制命令] --> B[必要时 SHUTDOWN_RESET]
    B --> C[mc_interface_try_input]
    C --> D{处于 DETECTING?}
    D -->|是| E[停止检测 PWM<br/>建立输入忽略窗口]
    D -->|否| F{仍在 ignore 窗口?}
    E --> F
    F -->|是| X[拒绝命令]
    F -->|否| G{接口已 lock?}
    G -->|是| H{override_once?}
    H -->|否| X
    H -->|是| I[消耗一次放行标志]
    G -->|否| J{控制后端初始化完成?}
    I --> J
    J -->|否| X
    J -->|是| K[方向修正及参数处理]
    K --> L{motor_type}
    L -->|BLDC / DC| M[调用 mcpwm_*]
    L -->|FOC| N[调用 mcpwm_foc_*]
    M --> O[记录 events]
    N --> O
```

`DIR_MULT` 根据 `m_invert_direction` 将目标值乘以 `+1` 或 `-1`。位置控制还会处理位置偏移、编码器反向配置和角度归一化。

相对电流接口把 `[-1, 1]` 映射到当前动态电流上下限。动态上限并非只来自持久化配置，还可能被温度、输入电压、RPM、占空比、功率和 BMS 条件进一步收紧。

## 7. 配置更新与后端切换

`mc_interface_set_configuration()` 不只是复制结构体：

```mermaid
flowchart TD
    A[收到新 mc_configuration] --> B{传感器端口模式变化?}
    B -->|是| C[encoder_deinit + encoder_init]
    B -->|否| D[encoder_update_config]
    C --> E[更新栅极驱动参数]
    D --> E
    E --> F{motor_type 变化?}
    F -->|是| G[mcpwm_deinit + mcpwm_foc_deinit]
    G --> H[复制新配置]
    H --> I{新 motor_type}
    I -->|BLDC / DC| J[mcpwm_init]
    I -->|FOC| K[mcpwm_foc_init]
    F -->|否| L[直接复制新配置]
    J --> M[更新动态限制]
    K --> M
    L --> M
    M --> N{当前后端}
    N -->|BLDC / DC| O[mcpwm_set_configuration]
    N -->|FOC| P[mcpwm_foc_set_configuration]
    O --> Q[bms_init]
    P --> Q
```

切换电机类型会重建底层定时器、ADC 和控制线程关系，不能视为普通参数写入。调用方应保证切换期间没有其他代码假定旧控制器仍在运行。

双电机 FOC 会同步两台电机的 `foc_f_zv` 和 `motor_type`，因为它们共享相关实时资源。

## 8. 双电机选择机制

大量 API 不带 `motor` 参数，目标电机由当前执行上下文确定：

```mermaid
flowchart TD
    A[mc_interface_motor_now] --> B{mcpwm_foc_isr_motor > 0?}
    B -->|是| C[使用当前 ISR 对应电机]
    B -->|否| D{当前线程 motor_selected > 0?}
    D -->|是| E[使用线程选择的电机]
    D -->|否| F[默认使用电机 1]
    C --> G[motor_now 返回 m_motor_1 或 m_motor_2]
    E --> G
    F --> G
```

`mc_interface_select_motor_thread(1/2)` 把选择保存在线程控制块的 `motor_selected` 字段中，所以不同线程可以同时选择不同电机。调用临时切换电机的函数必须在返回前恢复原选择。

优先级顺序是“ISR 电机 > 当前线程选择 > 电机 1 默认值”。这样 ADC/PWM 中断不会错误继承被打断线程的目标电机。

## 9. 实时中断公共路径

具体后端在每个控制周期调用 `mc_interface_mc_timer_isr(is_second_motor, dt)`。它把与控制算法无关但需要实时更新的工作集中在接口层：

```mermaid
flowchart LR
    T[ADC/PWM 定时事件] --> CTRL[mcpwm 或 mcpwm_foc 快环]
    CTRL --> IFISR[mc_interface_mc_timer_isr]
    IFISR --> VIN[输入电压滤波与越限积分]
    IFISR --> LIMIT[读取状态并执行公共限制]
    IFISR --> ENERGY[电流、Ah、Wh 与运行周期累计]
    IFISR --> TEMP[温度与栅极电压采样]
    IFISR --> SAMPLE[调试波形采集]
    IFISR --> CALLBACK[PWM 周期回调]
    VIN --> FAULT{发现故障?}
    LIMIT --> FAULT
    TEMP --> FAULT
    FAULT -->|是| EVENT[通知 Fault Stop 线程]
```

该函数运行在中断上下文，不能执行阻塞操作。`mc_interface_set_pwm_callback()` 注册的函数也在中断中调用，回调必须短小、确定且只能使用 ISR 安全的 API。

`mc_interface_adc_inj_int_handler()` 目前仅在 `MOTOR_TYPE_BLDC/DC` 时转发到 `mcpwm_adc_inj_int_handler()`；FOC 的主要 ADC 路径由 `mcpwm_foc` 自身处理。

## 10. 慢速监控与动态限幅

`mcif timer` 每 1 ms 对每台电机调用 `run_timer_tasks()`：

```mermaid
flowchart TB
    A[mcif timer 每 1 ms] --> B[选择目标电机]
    B --> C[更新慢速输入电压滤波]
    C --> D[更新里程和运行时间备份]
    D --> E[递减输入忽略与故障计数]
    E --> F[检查栅极驱动故障]
    F --> G[update_override_limits]
    G --> H[更新辅助输出]
    H --> I[encoder_check_faults]
    I --> J[检查电流传感器偏置]
    J --> K[检查三相电流不平衡]
    K --> L[可选轮速传感器更新]
```

`update_override_limits()` 计算配置中的运行时覆盖上限 `lo_current_max/min` 等。其输入包括母线电压、温度、转速、占空比、功率、电池限制和硬件额定值。后端使用这些覆盖值，而不是无条件使用静态配置上限。

快速过流等必须在 PWM/ADC 中断处理；温升和偏置漂移等允许毫秒级响应的条件放在线程中处理。

## 11. 故障处理机制

`mc_interface_fault_stop()` 可以从中断或线程调用。中断侧只记录故障参数并用 I-Class 事件唤醒高优先级故障线程。

```mermaid
sequenceDiagram
    participant SRC as ISR 或监控线程
    participant API as mc_interface_fault_stop
    participant EVT as ChibiOS 事件
    participant FT as Fault Stop 线程
    participant CTRL as 控制后端
    participant LOG as 日志/命令系统

    SRC->>API: fault, motor, is_isr
    API->>API: 写入 m_fault_data
    alt 来自 ISR
        API->>EVT: chEvtSignalI
    else 来自线程
        API->>EVT: chEvtSignal
    end
    EVT-->>FT: 唤醒
    FT->>FT: 选择故障对应电机
    FT->>FT: 设置 ignore 窗口与 m_fault_now
    FT->>CTRL: 停止/释放电机输出
    FT->>LOG: 保存电流、电压、占空比等故障快照
```

这种结构把“快速发现”与“完整处理”分离：ISR 立即提交停机事件，线程负责日志、状态整理及可能涉及非 ISR 安全 API 的后续工作。

`m_fault_data` 是单个共享槽位，不是故障队列。如果多个故障在故障线程处理前连续覆盖，线程处理的是最后复制到的内容；实时保护仍应由底层快速停机逻辑保证。

## 12. ADC/PWM 调试采样

模块在 `.ram4` 中保存多组固定长度采样数组，包括三相电流、三相电压、虚拟中点、滤波电流、开关频率、状态和相位。

```mermaid
flowchart LR
    CMD[mc_interface_sample_print_data] --> CFG[配置模式、长度、抽取率和回调]
    CFG --> ISR[ADC/PWM 中断采样]
    ISR --> BUF[RAM4 环形/定长采样数组]
    BUF --> TRIG{立即完成或触发条件满足?}
    TRIG -->|否| ISR
    TRIG -->|是| EVT[chEvtSignalI]
    EVT --> SEND[SampleSender 线程]
    SEND --> PACK[send_sample_block 打包]
    PACK --> CB[调用 reply_func 发送]
```

支持立即采样、启动触发、故障触发以及不自动发送等模式。中断只负责采集和通知，打包与通信在线程中执行，从而避免显著拉长控制中断。

## 13. 统计、计量与派生量

接口层将后端原始状态转换为上层更容易使用的量：

- 根据电流与 `dt` 累计放电/回充 Ah；
- 根据输入电压和输入电流累计 Wh；
- 根据转速计、电机极数、齿比和轮径计算速度与距离；
- 结合电池配置估算剩余电量；
- 汇总功率、速度、电流、MOSFET 温度和电机温度的平均值及最大值；
- 将当前配置和测量值聚合为 `setup_values`。

```mermaid
flowchart LR
    RAW["后端原始量<br/>RPM、电流、占空比、tachometer"] --> IF[mc_interface 换算与累计]
    CFG["配置<br/>极数、齿比、轮径、电池容量"] --> IF
    ADC["ADC<br/>输入电压、温度"] --> IF
    IF --> TELE["上层遥测<br/>速度、距离、Ah、Wh、电池余量"]
    IF --> STATS["统计<br/>平均值、最大值、累计时间"]
```

带 `reset` 参数的读取接口会在返回累计值后清零对应累加器。并发调用这些接口时，应明确数据的唯一消费者，避免某个调用者提前清零另一个调用者需要的数据。

## 14. 锁、忽略窗口与释放语义

三种机制容易混淆：

| 机制 | 含义 |
|---|---|
| `mc_interface_lock()` | 持续拒绝当前电机的大多数新控制命令 |
| `mc_interface_lock_override_once()` | 在锁定状态下允许下一条命令通过，然后自动清除 |
| `mc_interface_ignore_input(ms)` | 在约 `ms` 个 1 ms 维护周期内拒绝命令 |
| `release_motor()` | 受正常准入检查影响的释放请求 |
| `release_motor_override()` | 绕过普通输入锁定，用于必须确保关闭输出的路径 |

这些是业务控制锁，不是 ChibiOS 互斥量，也不提供对共享内存的并发互斥。需要保护数据结构时仍应使用正确的内核同步或临界区。

## 15. 依赖关系

```mermaid
flowchart TB
    IF[mc_interface.c]
    MC[mcpwm]
    FOC[mcpwm_foc]
    ENC[encoder]
    CONF[conf_general + confgenerator]
    HW[hw.h + STM32 HAL]
    RTOS[ChibiOS]
    AUX[commands、events、BMS、shutdown、mempools]
    UTIL[utils_math、utils_sys、buffer、CRC、timer]

    IF --> MC
    IF --> FOC
    IF --> ENC
    IF --> CONF
    IF --> HW
    IF --> RTOS
    IF --> AUX
    IF --> UTIL
```

这种依赖广度说明 `mc_interface` 是协调层而不是纯算法库。修改它的初始化、故障或实时中断代码，可能同时影响控制、通信、配置存储和硬件安全。

## 16. 常见调用路径

### 16.1 电流控制

```mermaid
sequenceDiagram
    participant APP as 应用/通信
    participant IF as mc_interface
    participant CTRL as mcpwm 或 mcpwm_foc
    participant ISR as ADC/PWM 中断
    participant MOTOR as 功率级与电机

    APP->>IF: mc_interface_set_current(current)
    IF->>IF: try_input + 方向处理
    IF->>CTRL: 设置后端电流目标
    Note over APP,CTRL: API 返回只表示目标已提交
    MOTOR-->>ISR: 下一次同步采样
    ISR->>CTRL: 执行电流控制/换相
    CTRL->>MOTOR: 更新 PWM
    ISR->>IF: 更新公共测量、限制和故障状态
```

### 16.2 查询转速

```mermaid
flowchart LR
    A[上层调用 mc_interface_get_rpm] --> B[motor_now 选择电机]
    B --> C{motor_type}
    C -->|BLDC / DC| D[mcpwm_get_rpm]
    C -->|FOC| E[mcpwm_foc_get_rpm]
    D --> F[乘以 DIR_MULT 后返回]
    E --> F
```

## 17. 修改与调试注意事项

1. **不要绕过统一接口。** 上层直接调用 `mcpwm_*` 或 `mcpwm_foc_*` 会跳过锁定、方向、事件记录和统一故障语义。
2. **区分 ISR 与线程 API。** `mc_interface_mc_timer_isr()`、PWM 回调和中断侧故障路径不能阻塞。
3. **保护快环预算。** 不要在实时中断路径加入日志打印、Flash 写入、动态内存或不定长循环。
4. **保留电机选择。** 临时选择另一台电机后必须恢复当前线程原来的 `motor_selected`。
5. **配置切换有副作用。** 传感器模式或电机类型改变会重初始化编码器和控制后端。
6. **动态限制与静态配置不同。** 排查“电流达不到配置值”时应检查 `lo_current_max/min`、温度、电压、功率、RPM 和 BMS 限制。
7. **回调上下文明确。** PWM 回调在中断执行，采样发送回调在 `SampleSender` 线程执行。
8. **故障信息槽不是队列。** 高频连续故障的日志顺序不能依赖 `m_fault_data` 保存全部事件。

## 18. 推荐阅读顺序

```mermaid
flowchart TB
    A[mc_interface.h<br/>按类别浏览公开 API] --> B[mc_interface.c 顶部<br/>状态结构、采样数组和线程]
    B --> C[mc_interface_init<br/>理解生命周期]
    C --> D[motor_now 与双电机选择]
    D --> E[set_configuration<br/>理解后端切换]
    E --> F[set_current/set_duty 等<br/>理解准入与分派]
    F --> G[mc_interface_try_input<br/>理解锁与忽略窗口]
    G --> H[mc_interface_mc_timer_isr<br/>理解实时公共路径]
    H --> I[run_timer_tasks/update_override_limits<br/>理解慢速保护]
    I --> J[fault_stop_thread 与采样线程]
    J --> K[mcpwm 或 mcpwm_foc<br/>继续追踪具体控制算法]
```

## 19. 小结

`mc_interface` 是电机子系统的统一门面和协调中心。它向上提供稳定的控制与遥测接口，向下按 `motor_type` 选择 BLDC/DC 或 FOC 后端，并横向协调配置、编码器、栅极驱动、BMS、故障、统计和调试采样。

理解该模块时应抓住四条主线：控制命令如何准入并路由、当前调用针对哪台电机、实时中断与后台线程如何分工、故障和动态限制如何覆盖普通控制目标。掌握这四条主线后，大多数电机控制行为都能从 `mc_interface` 继续追踪到明确的后端实现。
