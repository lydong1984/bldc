# `foc_math.c` 模块设计流程图

## 1. 文档范围

本文档描述 `motor/foc_math.c` 中各算法函数的设计流程，流程图使用 Mermaid 表示。接口和共享状态结构定义于 `motor/foc_math.h`，函数由 `motor/mcpwm_foc.c` 的初始化流程、FOC 实时控制流程和 PID 控制线程调用。

该模块不拥有独立任务或中断，主要承担以下计算职责：

- 电机磁链观测与转子电角度估算；
- PLL 角度跟踪和速度估算；
- 空间矢量调制 SVM；
- 位置环、速度环和弱磁控制；
- 编码器、霍尔传感器与无感观测角度的切换或融合；
- HFI 角度修正和 FOC 常用参数预计算。

## 2. 模块总体设计

```mermaid
flowchart LR
    CFG["电机配置<br/>mc_configuration"]
    STATE["电机运行状态<br/>motor_all_state_t"]
    INIT["初始化或配置更新"]
    ISR["FOC 实时控制流程"]
    CTRL["位置与速度控制线程"]

    INIT --> PRE["foc_precalc_values<br/>预计算控制常量"]
    PRE --> STATE

    ISR --> OBS["foc_observer_update<br/>磁链和观测角度"]
    OBS --> ANGLE["传感器角度选择与融合"]
    ANGLE --> ENC["foc_correct_encoder"]
    ANGLE --> HALL["foc_correct_hall"]
    ENC --> PLL["foc_pll_run<br/>角度和速度跟踪"]
    HALL --> PLL

    ISR --> HFI["foc_hfi_adjust_angle<br/>低速角度修正"]
    ISR --> FW["foc_run_fw<br/>弱磁电流目标"]
    ISR --> SVM["foc_svm<br/>三相 PWM 比较值"]

    CTRL --> POS["foc_run_pid_control_pos<br/>位置环输出 iq 目标"]
    CTRL --> SPD["foc_run_pid_control_speed<br/>速度环输出 iq 目标"]

    CFG --> PRE
    CFG --> OBS
    CFG --> ANGLE
    CFG --> PLL
    CFG --> HFI
    CFG --> FW
    CFG --> POS
    CFG --> SPD

    STATE <--> OBS
    STATE <--> ANGLE
    STATE <--> PLL
    STATE <--> HFI
    STATE <--> FW
    STATE <--> POS
    STATE <--> SPD
```

> 上图表示职责和主要数据关系，不表示每次控制周期一定按图中所有节点顺序执行。实际调用由 `mcpwm_foc.c` 根据硬件配置、传感器模式和控制模式选择。

### 2.1 函数接口摘要

| 函数 | 主要输入 | 输出或关键状态更新 | 设计目的 |
|---|---|---|---|
| `foc_observer_update` | αβ 电压、电流、`dt` | `observer_state`、可选 `phase` | 估算磁链矢量和转子电角度 |
| `foc_pll_run` | 测量角度、`dt` | `phase_var`、`speed_var` | 连续跟踪角度并估算电角速度 |
| `foc_svm` | αβ 调制量、最大调制率、PWM 周期 | A/B/C 相比较值、扇区 | 将 αβ 电压矢量映射为三相 PWM |
| `foc_run_pid_control_pos` | 位置目标、当前位置、`dt` | `m_iq_set` 和位置 PID 状态 | 位置误差到 q 轴电流目标 |
| `foc_run_pid_control_speed` | 速度目标、速度估计、`dt` | `m_iq_set` 和速度 PID 状态 | 速度误差到 q 轴电流目标 |
| `foc_correct_encoder` | 观测角、编码器角、转速 | `m_using_encoder`、选定角度 | 带迟滞地切换编码器和无感角度 |
| `foc_correct_hall` | 观测角、霍尔值、`dt` | 霍尔插值状态、融合角度 | 霍尔角重建、限速及过渡融合 |
| `foc_run_fw` | 运行状态、占空比、q 轴电流误差 | `m_i_fw_set` | 高占空比区域生成弱磁电流目标 |
| `foc_hfi_adjust_angle` | HFI 角误差、`dt` | HFI 角度、双积分器、就绪标志 | 通过误差反馈修正低速估算角度 |
| `foc_precalc_values` | 电机参数、PWM 配置 | `p_*` 预计算字段、初始磁链 | 降低实时控制路径的重复计算量 |

## 3. 磁链观测器 `foc_observer_update`

### 3.1 主流程

```mermaid
flowchart TD
    S([开始]) --> LOAD["读取配置参数<br/>R、L、lambda"]
    LOAD --> SAT{"磁饱和补偿模式"}

    SAT -->|SAT_COMP_LAMBDA| SL["支持磁链补偿的观测器<br/>L 按 lambda_est 与 lambda 比值修正"]
    SAT -->|SAT_COMP_FACTOR| SF["按滤波电流计算补偿系数<br/>同时减小 L 和 lambda"]
    SAT -->|SAT_COMP_LAMBDA_AND_FACTOR| SBF["先按估算磁链修正 L<br/>再按电流补偿系数减小 L"]
    SAT -->|其他| SN["保持原始 L 和 lambda"]

    SL --> TEMP
    SF --> TEMP
    SBF --> TEMP
    SN --> TEMP

    TEMP{"启用温度补偿?"}
    TEMP -->|是| RT["R = m_res_temp_comp"]
    TEMP -->|否| SAL
    RT --> SAL

    SAL{"abs(id) 或 abs(iq)<br/>大于 0.1?"}
    SAL -->|是| LADJ["按 d/q 电流和凸极差<br/>修正等效电感 L"]
    SAL -->|否| PREP
    LADJ --> PREP

    PREP["计算 L_ia、L_ib、R_ia、R_ib<br/>gamma_half"] --> TYPE{"观测器类型"}
    TYPE -->|Ortega Original| O1["执行 Ortega 原始观测器"]
    TYPE -->|MXLEMMING 系列| O2["执行 MXLEMMING 观测器"]
    TYPE -->|Ortega Lambda Comp| O3["执行磁链补偿 Ortega 观测器"]
    TYPE -->|MXV 系列| O4["执行 MXV 观测器"]
    TYPE -->|其他| COMMON

    O1 --> COMMON["保存本次 i_alpha 和 i_beta"]
    O2 --> COMMON
    O3 --> COMMON
    O4 --> COMMON
    COMMON --> NAN["将 x1、x2 的 NaN 置零"]
    NAN --> MAG{"磁链状态幅值<br/>小于 0.5 倍 lambda?"}
    MAG -->|是| BOOST["x1、x2 同时乘 1.1<br/>避免角度在低幅值时不稳定"]
    MAG -->|否| PHASE
    BOOST --> PHASE{"phase 指针有效?"}
    PHASE -->|是| ATAN["phase = atan2<br/>x2 - L_ib, x1 - L_ia"]
    PHASE -->|否| E([结束])
    ATAN --> E
```

### 3.2 各观测器分支

```mermaid
flowchart TD
    T{"观测器分支"}

    T -->|Ortega Original| OA["err = lambda² - 磁链误差模平方"]
    OA --> OB["限制 err 不大于 0"]
    OB --> OC["计算 x1_dot、x2_dot<br/>并按 dt 积分 x1、x2"]

    T -->|MXLEMMING| MA["积分 v - R i<br/>减去 L 乘电流增量"]
    MA --> MB{"Lambda Comp 变体?"}
    MB -->|是| MC["更新并限制 lambda_est<br/>按 lambda_est 限制 x1、x2"]
    MB -->|否| MD["按配置 lambda 限制 x1、x2"]
    MC --> ME["L_ia = 0，L_ib = 0<br/>复用统一 atan2 计算"]
    MD --> ME

    T -->|Ortega Lambda Comp| LA["以 lambda_est 计算误差"]
    LA --> LB["更新 lambda_est<br/>限制到 0.3 至 2.5 倍 lambda"]
    LB --> LC["限制 err 不大于 0"]
    LC --> LD["计算状态导数<br/>并按 dt 积分 x1、x2"]

    T -->|MXV 系列| XA["积分 v - R i 得到 x1、x2"]
    XA --> XB{"具体 MXV 类型"}
    XB -->|Lambda Comp Lin| XC["低通跟踪磁链幅值<br/>限制 lambda_est"]
    XC --> XD["幅值超限时按 lambda_est<br/>同比例缩放 x1、x2"]
    XB -->|Lambda Comp| XE["按平方误差更新 lambda_est<br/>并限制范围"]
    XE --> XD
    XB -->|基础 MXV| XF["幅值超过 lambda 时<br/>同比例缩放 x1、x2"]

    OC --> END([返回公共收尾流程])
    ME --> END
    LD --> END
    XD --> END
    XF --> END
```

观测器的核心输出状态是 `x1`、`x2` 和 `lambda_est`。其中 `i_alpha_last`、`i_beta_last` 仅供 MXLEMMING 离散电流增量项使用；最终角度由修正后的 αβ 磁链矢量计算。

## 4. PLL 跟踪 `foc_pll_run`

```mermaid
flowchart TD
    S([开始]) --> P0["phase_var 的 NaN 置零"]
    P0 --> ERR["delta_theta = phase - phase_var"]
    ERR --> NERR["将 delta_theta 归一化到角度周期"]
    NERR --> S0["speed_var 的 NaN 置零"]
    S0 --> PH["phase_var +=<br/>speed_var + kp × delta_theta<br/>再乘 dt"]
    PH --> NPH["归一化 phase_var"]
    NPH --> SP["speed_var +=<br/>ki × delta_theta × dt"]
    SP --> E([结束])
```

PLL 的比例通道直接修正相位推进速度，积分通道形成连续的电角速度估计。

## 5. 空间矢量调制 `foc_svm`

### 5.1 扇区判定

```mermaid
flowchart TD
    S([输入 alpha、beta]) --> B{"beta 大于等于 0?"}
    B -->|是| AP{"alpha 大于等于 0?"}
    B -->|否| AN{"alpha 大于等于 0?"}

    AP -->|是| Q1{"beta / sqrt(3)<br/>大于 alpha?"}
    Q1 -->|是| SEC2["扇区 2"]
    Q1 -->|否| SEC1["扇区 1"]

    AP -->|否| Q2{"-beta / sqrt(3)<br/>大于 alpha?"}
    Q2 -->|是| SEC3["扇区 3"]
    Q2 -->|否| SEC2

    AN -->|是| Q4{"-beta / sqrt(3)<br/>大于 alpha?"}
    Q4 -->|是| SEC5["扇区 5"]
    Q4 -->|否| SEC6["扇区 6"]

    AN -->|否| Q3{"beta / sqrt(3)<br/>大于 alpha?"}
    Q3 -->|是| SEC4["扇区 4"]
    Q3 -->|否| SEC5
```

### 5.2 PWM 比较值生成

```mermaid
flowchart TD
    S([已确定扇区]) --> CALC{"按扇区计算有效矢量时间<br/>并生成居中对称的 tA、tB、tC"}
    CALC -->|扇区 1| C1["计算 t1、t2<br/>A → B → C 依次减去矢量时间"]
    CALC -->|扇区 2| C2["计算 t2、t3<br/>以 B 相为上基准"]
    CALC -->|扇区 3| C3["计算 t3、t4<br/>以 B 相为上基准"]
    CALC -->|扇区 4| C4["计算 t4、t5<br/>以 C 相为上基准"]
    CALC -->|扇区 5| C5["计算 t5、t6<br/>以 C 相为上基准"]
    CALC -->|扇区 6| C6["计算 t6、t1<br/>以 A 相为上基准"]

    C1 --> LIMIT
    C2 --> LIMIT
    C3 --> LIMIT
    C4 --> LIMIT
    C5 --> LIMIT
    C6 --> LIMIT

    LIMIT["t_max = PWM 周期 ×<br/>{1 - 0.5 × (1 - max_mod)}"] --> CLAMP["分别将 tA、tB、tC<br/>限制到 0 至 t_max"]
    CLAMP --> OUT["输出三相比较值和扇区"]
    OUT --> E([结束])
```

输入调制矢量的幅值应不超过 `sqrt(3) / 2`，即约 `0.866`，以避免过调制。`max_mod` 还会进一步限制三相比较值的上界。

## 6. 位置 PID `foc_run_pid_control_pos`

```mermaid
flowchart TD
    S([开始]) --> MODE{"当前为位置控制模式?"}
    MODE -->|否| RESET["清零 I 项、历史误差和 D 滤波状态<br/>记录当前位置后返回"]
    RESET --> E([结束])

    MODE -->|是| ERR["计算目标角与当前角的周期误差"]
    ERR --> SIGN{"传感器不是霍尔<br/>且编码器反向?"}
    SIGN -->|是| NEG["误差方向乘 -1"]
    SIGN -->|否| GAIN
    NEG --> GAIN["读取 kp、ki、kd、kd_proc"]

    GAIN --> DEC{"启用小角度增益衰减<br/>且误差小于阈值?"}
    DEC -->|是| SCALE["按误差比例同步缩小<br/>P、I、D 和过程 D 增益"]
    DEC -->|否| PI
    SCALE --> PI["计算 P 项并积分 I 项"]

    PI --> ED{"误差与上次相同?"}
    ED -->|是| DZ["误差 D 项 = 0"]
    ED -->|否| DC["使用累计 dt 计算误差 D 项<br/>随后清零该累计时间"]
    DZ --> DF
    DC --> DF["低通滤波误差 D 项"]

    DF --> PD{"当前位置与上次处理值相同?"}
    PD -->|是| PZ["过程 D 项 = 0"]
    PD -->|否| PC["由位置变化率计算过程 D 项<br/>随后清零对应累计时间"]
    PZ --> PDF
    PC --> PDF["低通滤波过程 D 项"]

    PDF --> AW["根据 P 项余量限制 I 项<br/>实现积分抗饱和"]
    AW --> SAVE["保存误差和当前位置"]
    SAVE --> OUT["output = P + I + D + D_process<br/>限制到 -1 至 1"]
    OUT --> HALL{"传感器模式为霍尔?"}
    HALL -->|是| IQ["iq_set = output × 最大电流 × 缩放"]
    HALL -->|否| IDX{"已找到编码器索引?"}
    IDX -->|是| IQ
    IDX -->|否| SEEK["iq_set = 40% 最大电流<br/>转动电机搜索索引"]
    IQ --> E
    SEEK --> E
```

位置环同时包含“误差微分”和“过程量微分”两路 D 项。累计 `dt` 的设计用于处理低速时位置量化导致的多个控制周期读数不变问题。

## 7. 速度 PID `foc_run_pid_control_speed`

```mermaid
flowchart TD
    S([开始]) --> MODE{"当前为速度控制模式?"}
    MODE -->|否| RESET["清零速度 I 项、历史误差和 D 滤波<br/>返回"]
    RESET --> E([结束])

    MODE -->|是| RAMP{"速度斜坡参数大于 0?"}
    RAMP -->|是| STEP["速度设定值按斜坡逼近命令值"]
    STEP --> IDX{"编码器索引未找到?"}
    IDX -->|是| OPEN["将设定值限制在开环转速范围"]
    IDX -->|否| LRPM
    OPEN --> LRPM["将设定值限制到允许 ERPM 范围"]
    RAMP -->|否| SRC
    LRPM --> SRC{"速度来源"}

    SRC -->|PLL| PLL["rpm = m_pll_speed 换算值"]
    SRC -->|FAST| FAST["rpm = m_speed_est_fast 换算值"]
    SRC -->|FASTER| FASTER["rpm = m_speed_est_faster 换算值"]
    PLL --> ERR
    FAST --> ERR
    FASTER --> ERR["error = 速度设定值 - rpm"]

    ERR --> LOW{"设定转速绝对值<br/>小于最小闭环转速?"}
    LOW -->|是| RELEASE["清零 I 项与 iq_set<br/>保存误差并返回"]
    RELEASE --> E
    LOW -->|否| PID["计算 P 项和 D 项"]
    PID --> DF["低通滤波 D 项并保存误差"]
    DF --> OUT["output = P + I + D<br/>限制绝对值不超过 1"]
    OUT --> INT["积分 I 项并限制绝对值不超过 1"]
    INT --> KIZ{"ki 接近 0?"}
    KIZ -->|是| IZ["强制 I 项为 0"]
    KIZ -->|否| BRK
    IZ --> BRK{"禁止制动?"}
    BRK -->|否| IQ
    BRK -->|是| OPP{"输出与当前转速方向相反<br/>且转速绝对值大于 20 rpm?"}
    OPP -->|是| ZERO["output = 0"]
    OPP -->|否| IQ
    ZERO --> IQ["iq_set = output × lo_current_max × 缩放"]
    IQ --> E
```

## 8. 编码器与观测角切换 `foc_correct_encoder`

```mermaid
flowchart TD
    S([开始]) --> RPM["rpm_abs = 电角速度绝对值换算为 rpm"]
    RPM --> H["hyst = 切换转速 sl_erpm 的 5%"]
    H --> USING{"当前正在使用编码器?"}
    USING -->|是| HIGH{"rpm_abs 大于<br/>sl_erpm + hyst?"}
    HIGH -->|是| OFF["m_using_encoder = false"]
    HIGH -->|否| RET
    OFF --> RET

    USING -->|否| LOW{"rpm_abs 小于<br/>sl_erpm - hyst?"}
    LOW -->|是| ON["m_using_encoder = true"]
    LOW -->|否| RET
    ON --> RET{"最终选择"}

    RET -->|使用编码器| ENC["返回 enc_angle"]
    RET -->|使用观测器| OBS["返回 obs_angle"]
```

上下两个不同阈值形成迟滞区，避免转速在切换点附近波动时角度来源频繁跳变。

## 9. 霍尔角修正与融合 `foc_correct_hall`

### 9.1 霍尔状态更新

```mermaid
flowchart TD
    S([开始]) --> DT["累加当前霍尔步的持续时间"]
    DT --> SPEED["计算 PLL 转速、霍尔步进速度<br/>并判断是否处于霍尔使用区"]
    SPEED --> LOOKUP["由 hall_val 查询 foc_hall_table"]
    LOOKUP --> VALID{"表值小于 201?"}

    VALID -->|否| INVALID["前一霍尔角标记为无效"]
    INVALID --> OVERRIDE{"观测角被开环覆盖<br/>且电机正在运行?"}
    OVERRIDE -->|是| OPA["angle = observer_override"]
    OVERRIDE -->|否| BLEND
    OPA --> BLEND

    VALID -->|是| CONV["将表值从 0 至 200<br/>映射为 0 至 2π"]
    CONV --> FIRST{"前一霍尔值无效?"}
    FIRST -->|是| INIT["初始化前一值和霍尔角"]
    FIRST -->|否| CHANGE{"霍尔值发生跳变?"}
    CHANGE -->|否| STORE
    CHANGE -->|是| DIFF["计算跨越 0 点后的最短差值"]
    DIFF --> DIR{"本次方向与上次方向一致?"}
    DIR -->|是| PERIOD["按方向保存带符号步进周期"]
    DIR -->|否| FLIP["反转上次步进周期的符号"]
    PERIOD --> MID
    FLIP --> MID["清零当前计时<br/>角度置于新旧霍尔角的中点"]
    INIT --> STORE
    MID --> STORE["保存当前霍尔表值"]
    STORE --> INTERP
```

### 9.2 插值、变化率限制与过渡融合

```mermaid
flowchart TD
    INTERP{"估算步进速度低于<br/>霍尔插值阈值?"}
    INTERP -->|是| DIRECT["低速不插值<br/>直接使用最近霍尔角"]
    INTERP -->|否| DELTA["计算插值角与当前霍尔中心角之差"]
    DELTA --> OK{"角差小于 30 度<br/>或角差方向与转速方向不同?"}
    OK -->|是| ADV["按霍尔估算角速度推进角度"]
    OK -->|否| PULL["按 1% 角差缓慢拉回"]

    DIRECT --> NORM
    ADV --> NORM
    PULL --> NORM["归一化霍尔插值角"]
    NORM --> STEP["根据霍尔转速或插值阈值<br/>计算本周期最大角度步长"]
    STEP --> CLOSE{"目标角差小于最大步长?"}
    CLOSE -->|是| SET["限速角直接跟随目标角"]
    CLOSE -->|否| MOVE["限速角按误差方向移动一个步长"]
    SET --> NRATE
    MOVE --> NRATE["归一化限速霍尔角"]
    NRATE --> USE{"当前应使用霍尔角?"}
    USE -->|是| ANG["angle = 限速霍尔角"]
    USE -->|否| BLEND
    ANG --> BLEND{"输出角与原观测角不同?"}
    BLEND -->|是| WEIGHT["按 foc_sl_erpm_start 到 foc_sl_erpm<br/>计算并限制霍尔权重"]
    WEIGHT --> MIX["在霍尔角与原观测角之间<br/>进行周期角插值"]
    BLEND -->|否| RET
    MIX --> RET["返回 angle"]
```

霍尔路径包含三层平滑机制：扇区内角度插值、每周期角度变化率限制，以及切换转速区间内与观测角的加权融合。无效霍尔读数不会更新霍尔角，并允许在满足条件时退回开环覆盖角度。

## 10. 弱磁控制 `foc_run_fw`

```mermaid
flowchart TD
    S([开始]) --> CAP{"最大弱磁电流小于<br/>有效最小控制电流?"}
    CAP -->|是| E([直接返回])
    CAP -->|否| ACTIVE{"电机正在运行，并且为电流、制动或速度模式<br/>或者之前弱磁电流仍大于最小电流?"}
    ACTIVE -->|否| E
    ACTIVE -->|是| ZERO["fw_current_now = 0<br/>读取滤波占空比绝对值"]
    ZERO --> DUTY{"弱磁起始占空比小于 0.99<br/>且当前占空比超过起始阈值?"}

    DUTY -->|否| RAMP
    DUTY -->|是| MAX["i_fw_max = 最大弱磁电流"]
    MAX --> BACK{"启用弱磁回退?"}
    BACK -->|是| ERR["根据速度方向和 iq 跟踪误差<br/>计算 0 至 1 的回退系数"]
    ERR --> REDUCE["按回退系数减小 i_fw_max"]
    BACK -->|否| MAP
    REDUCE --> MAP["将占空比从起始阈值到最大值<br/>映射为 0 至 i_fw_max"]
    MAP --> DELAY["m_current_off_delay = 1<br/>防止退出弱磁后过早停止调制"]
    DELAY --> RAMP{"弱磁斜坡时间小于 dt?"}

    RAMP -->|是| DIRECT["m_i_fw_set = fw_current_now"]
    RAMP -->|否| STEP["按配置斜坡速度逼近 fw_current_now"]
    DIRECT --> E
    STEP --> E
```

回退逻辑在 `iq` 超过目标且可能挤占 q 轴电压裕量时降低弱磁请求，用于减小过度弱磁导致的失控风险。

## 11. HFI 角度修正 `foc_hfi_adjust_angle`

```mermaid
flowchart TD
    S([开始]) --> LIMIT["将 ang_err 限制到<br/>正负 foc_hfi_max_err"]
    LIMIT --> GAIN["计算一次反馈增益 gain_int<br/>和二次积分增益 gain_int2"]
    GAIN --> INT["double_integrator +=<br/>ang_err × gain_int2"]
    INT --> CLAMP["将 double_integrator 限制到<br/>当前快速速度估计的绝对值"]
    CLAMP --> ANG["HFI angle -= dt ×<br/>gain_int × ang_err + double_integrator"]
    ANG --> NORM["将 HFI angle 归一化到角度周期"]
    NORM --> READY["ready = true"]
    READY --> E([结束])
```

该函数以受限角误差驱动比例反馈和双积分状态，输出连续的 HFI 角度；`ready` 表示角度修正链路已产生有效更新。

## 12. 参数预计算 `foc_precalc_values`

```mermaid
flowchart TD
    S([开始]) --> L["由平均电感和凸极差计算<br/>p_lq 与 p_ld"]
    L --> INV["计算 p_inv_ld_lq<br/>和 p_v2_v3_inv_avg_half"]
    INV --> FLUX["lambda_est 初始化为<br/>配置的永磁体磁链"]
    FLUX --> DUTY["p_duty_norm =<br/>2 / sqrt(3) / 过调制因子"]
    DUTY --> SHUNT{"编译时定义<br/>HW_HAS_PHASE_SHUNTS?"}
    SHUNT -->|否| HALF["p_fs = foc_f_zv × 0.5"]
    SHUNT -->|是| SAMPLE{"采样模式为 V0_V7?"}
    SAMPLE -->|是| FULL["p_fs = foc_f_zv"]
    SAMPLE -->|否| HALF
    FULL --> DT
    HALF --> DT["p_dt = 1 / p_fs"]
    DT --> E([结束])
```

此函数在电机初始化及配置更新后执行。`p_fs` 的取值取决于相分流硬件能力和控制采样模式，`p_dt` 则作为对应采样周期供实时算法使用。

## 13. 关键状态与约束

```mermaid
flowchart LR
    INPUT["采样输入<br/>v_alpha、v_beta<br/>i_alpha、i_beta"] --> OBS["observer_state<br/>x1、x2、lambda_est"]
    OBS --> SENSOR["角度来源切换或融合<br/>编码器、霍尔、HFI"]
    SENSOR --> TRACK["PLL 状态<br/>m_pll_phase、m_pll_speed"]
    TRACK --> PID["位置或速度 PID"]
    PID --> IQ["m_iq_set"]
    TRACK --> FW["弱磁控制"]
    FW --> IFW["m_i_fw_set"]
    IQ --> CURRENT["FOC 电流调节器<br/>位于 mcpwm_foc.c"]
    IFW --> CURRENT
    CURRENT --> MOD["mod_alpha_raw、mod_beta_raw"]
    MOD --> SVM["foc_svm"]
    SVM --> PWM["tA、tB、tC"]
```

- 角度均为电角度，涉及周期差值时必须使用角度归一化或周期角插值函数。
- 速度状态主要以电角速度表示，需要通过 `RADPS2RPM_f` 才得到代码中用于阈值判断的 ERPM/rpm 数值。
- `motor_all_state_t` 中 PID、霍尔、HFI 和观测器历史量会跨调用保存，不能把这些函数视为无状态纯函数。
- `dt` 必须与调用周期一致；PLL、观测器、PID 微积分项、霍尔插值、弱磁斜坡和 HFI 修正均直接依赖它。
- SVM 输出在写入硬件前已经按 `max_mod` 限幅，但调用方仍需保证输入调制矢量满足其幅值约束。
