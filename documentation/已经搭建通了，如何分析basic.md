好,环境通了就进入实战。下面给你一套**系统分析 `basic` 的方法**,从"它是怎么被定义的"一路追到"代码怎么跑起来"。

## 第一步:先定位 basic 到底用了哪些文件

```bash
# 在 bldc 仓库根目录
grep -rn "basic" Makefile makefiles/ 2>/dev/null
```

VESC 的 Makefile 体系里,每个目标都会指定它的硬件头文件和源文件。你要找到类似这样的定义:

```makefile
basic_FW := ...
# 或者在 makefiles/ 下某个 .mk 文件里指向 hwconf 的某个 .h/.c
```

找到后记下两个关键文件路径,通常形如:

```
hwconf/<某目录>/hw_basic.h
hwconf/<某目录>/hw_basic.c
```

如果 grep 出来的信息不直观,直接看编译产物的中间文件:

```bash
make clean
make basic V=1 2>&1 | head -50
```

`V=1` 会打印完整编译命令,你能清楚看到编译器带了哪个 `-DHW_SOURCE=...` 或 `-include` 参数,从而确认 basic 实际用的硬件文件。

## 第二步:读硬件头文件(对照上次讲的宏分类)

打开找到的 `hw_basic.h`,按这个顺序扫:

```bash
# 快速提取所有宏定义,先看全貌
grep "#define" hwconf/<路径>/hw_basic.h
```

重点确认这几个值,它们决定了 basic 的"性格":

- `HW_NAME` —— 确认身份
- `CURRENT_SHUNT_RES` —— 采样电阻,判断它适合多大电流
- `VIN_R1` / `VIN_R2` —— 电压采样范围
- `HW_LIM_*` 系列 —— 安全边界
- `HW_HAS_*` 系列 —— 它假定有哪些硬件外设

把这些值抄下来,你就有了一张"basic 配置画像"。

## 第三步:追初始化流程(从 main 进去)

理解一块固件怎么跑,标准路径是从入口往下追:

```bash
# 找到入口
grep -rn "int main" *.c
```

打开 `main.c`,关注初始化顺序,典型是:

```
main()
 ├─ hw_init_gpio()        // 引脚初始化(在 hw_basic.c 里)
 ├─ conf_general_init()   // 加载配置
 ├─ mc_interface_init()   // 电机控制接口初始化
 ├─ comm_xxx_init()       // 通信(USB/UART/CAN)
 └─ 进入主循环 / 启动 RTOS 线程
```

VESC 用 ChibiOS(RTOS),所以 `main` 之后很多逻辑跑在各自的线程里。你可以搜:

```bash
grep -rn "chThdCreateStatic\|THD_FUNCTION" *.c | head
```

看它创建了哪些线程(通信线程、采样线程、定时任务等),这是理解 VESC 运行结构的关键。

## 第四步:追电机控制核心

这是 VESC 的灵魂,文件大概在 `motor/` 目录:

- `mc_interface.c` —— 对外的电机控制接口(设置电流、转速、占空比等入口)
- `mcpwm_foc.c` —— FOC 算法主体(磁场定向控制)
- `mcpwm.c` —— 传统 BLDC 六步换相

建议从一个**具体动作**反向追,比如"设置电流"是怎么一路传到硬件 PWM 的:

```bash
grep -rn "mc_interface_set_current" motor/ *.c
```

顺着这个函数往下读,你会看到它如何调用 FOC 模块、最终写到定时器寄存器产生 PWM。这种"沿着一条数据流读"的方式,比从头到尾通读高效得多。

## 第五步:做可验证的小实验

光读不够,改一处再编译验证,建立因果直觉:

```bash
# 例如改 HW_NAME 看 VESC Tool 显示是否变化
# 或在 main.c 初始化处加一行,改某个默认值
make clean && make basic
```

更进阶一点,可以看反汇编或符号表,确认你改的东西真进了固件:

```bash
arm-none-eabi-nm build/basic/basic.elf | grep <你关心的符号>
# 或看 map 文件
ls build/basic/*.map
```

## 推荐的分析路线总结

我的建议是按"**静态配置 → 启动流程 → 控制核心 → 动手验证**"四层推进:先用 grep 把 basic 的配置画像摸清楚(第一、二步),再从 main 理清它启动后创建了哪些线程(第三步),然后挑一条电机控制的数据流深入追(第四步),最后改代码编译验证闭环(第五步)。

---

为了帮你精确分析,**最有用的是你把第一步 grep 的结果,以及找到的 `hw_basic.h` 内容贴给我**。这样我就能针对你这个具体版本,告诉你 basic 的采样电阻是多少、它适合驱动什么量级的电机、各个限制值意味着什么——直接进入精读。

你想先从哪一层切入?是想彻底搞懂**配置宏**,还是更想跟着**电机控制的数据流**走一遍?
找到了,而且结构很清晰。先帮你解读这个目录,你就明白 basic 是个"系列"而不是单一配置。

## 目录结构解读

```
hwconf/vesc/basic/
├── hw_basic_core.h          # 核心公共声明(引脚、外设、函数原型)
├── hw_basic_core.c          # 核心公共实现(引脚初始化等)
├── hw_basic.h               # 总入口/分发头,根据宏选择具体变体
├── hw_basic_025.h           # 变体:采样电阻 0.25 mΩ (0.00025Ω)
├── hw_basic_035.h           # 变体:采样电阻 0.35 mΩ (0.00035Ω)
└── hw_basic_035_no_limits.h # 变体:0.35mΩ + 放开软件限制
```

这正是我之前讲的设计模式:**core 文件放公共部分(引脚映射、外设、初始化),各变体头文件只覆盖差异部分(主要是采样电阻和限制值)**。

**关键判断**:`025` / `035` 这两个数字几乎可以肯定指 **电流采样电阻阻值**:

- `025` → 0.00025Ω(0.25毫欧)
- `035` → 0.00035Ω(0.35毫欧)

这两个都是**很小的采样电阻**,典型用于**中大电流**场景。这对你的 2218 小电机是个需要注意的信号——回头细说。

## 下一步:把关键文件内容拉出来

请依次跑这几条,把输出贴给我:

```bash
# 1. 总入口,看它怎么分发到各变体
cat hwconf/vesc/basic/hw_basic.h

# 2. 核心公共配置(引脚、外设、ADC等)
cat hwconf/vesc/basic/hw_basic_core.h

# 3. 看看 basic 默认编译的是哪个变体(025还是035)
make basic V=1 2>&1 | grep -i "basic_0\|HW_HEADER\|HW_SOURCE\|-DHW"
```

如果第3条没输出,就先单独看两个变体差异:

```bash
cat hwconf/vesc/basic/hw_basic_035.h
cat hwconf/vesc/basic/hw_basic_025.h
```

## 我会怎么带你读

拿到内容后,我会按这个顺序逐宏精读:

先从 `hw_basic.h` 看清**分发逻辑**(它用什么宏来选 025/035/no_limits),再读 `hw_basic_core.h` 把**引脚映射、ADC 通道、栅极驱动芯片、电压采样**这些公共宏一个个讲清楚,最后对比三个变体头文件,讲明白**采样电阻和各 `HW_LIM_*` 限制值**的差异,以及 `no_limits` 到底放开了什么。

读完这一轮,你不仅能完全看懂 basic,还能自己判断它能不能驱动 2218、需不需要改采样电阻配置。

先把上面 1、2 两个 `cat` 的内容贴给我吧。文件可能有点长,没关系,贴全我才能逐行讲。
```bash
lee@DESKTOP-QG45PR9:~$ git clone https://github.com/vedderb/bldc.git
Cloning into 'bldc'...
remote: Enumerating objects: 34302, done.
remote: Counting objects: 100% (233/233), done.
remote: Compressing objects: 100% (104/104), done.
remote: Total 34302 (delta 159), reused 129 (delta 129), pack-reused 34069 (from 2)
Receiving objects: 100% (34302/34302), 95.42 MiB | 5.93 MiB/s, done.
Resolving deltas: 100% (23597/23597), done.
lee@DESKTOP-QG45PR9:~$ cd ./b
bag_files/ bldc/
lee@DESKTOP-QG45PR9:~$ cd ./bldc/
lee@DESKTOP-QG45PR9:~/bldc$
lee@DESKTOP-QG45PR9:~/bldc$ make
**WARNING** ARM-SDK not in /home/lee/bldc/tools/gcc-arm-none-eabi-7-2018-q2-update  Please run 'make arm_sdk_install'

   This Makefile is known to work on Linux and Mac in a standard shell environment.

   Here is a summary of the available targets:

   [Tool Installers]
     arm_sdk_install      - Install the GNU ARM gcc toolchain
     qt_install           - Install the all tools for Qt

   [Big Hammer]
     all_fw               - Build firmware for all boards
     all_fw_package       - Packaage firmware for boards in package list

   [Unit Tests]
     all_ut               - Build all unit tests
     all_ut_xml           - Run all unit tests and capture all XML output to files
     all_ut_run           - Run all unit tests and dump XML output to console

   [Firmware]
     fw   - Build firmware for default target
                            supported boards are: 100_250 100_250_mkiii 100_250_mkiii_no_limits 100_250_no_limits 100_500 100_500_no_limits 140_300 40 410 410_0005ohm 410_005ohm 410_no_limits 45 46 46_0005ohm 46_33k 48 49 60 60_75 60_75_mk2 60_75_mk2_no_limits 60_75_no_limits 60_mk3 60_mk3_no_limits 60_mk4 60_mk4_no_limits 60_mk5 60_mk5_no_limits 60_mk6 60_mk6_hp 60_mk6_hp_no_limits 60_mk6_max 60_mk6_no_limits 60_no_limits 60v2_alva 60v2_alva_mk1 60v2_alva_mk2 75_100 75_100_V2 75_100_V2_0005ohm 75_100_V2_005ohm 75_100_V2_no_limits 75_100_no_limits 75_300 75_300_mkiv 75_300_mkiv_no_limits 75_300_no_limits 75_300_r2 75_300_r2_no_limits 75_300_r3 75_300_r3_no_limits 75_600 75_600_no_limits Cheap_FOCer_2 Cheap_FOCer_2_V09 Cheap_FOCer_2_V09_no_limits Cheap_FOCer_2_no_limits ENNOID_150V_mk8 FOCSTrot_V3 FOCSTrot_V4 JetFleetF6_20s JetFleetF6_24s JetFleetF6_32s Lightning400 Little_FOCer Little_FOCer_V3 Little_FOCer_V3_1 Little_FOCer_V4 RSR_DD_V1 RSR_DD_V1_005 RSR_DD_V2 RSR_DD_V2.1 TRONIC_250R Thor300_20s Thor301 Thor400 Thor400v2 a100s_v4 a200s_v2.1 a200s_v2.2 a200s_v3 a200s_v4 a200s_v41 a50s_dual_12s a50s_dual_8s a50s_v22_12s a50s_v22_12s_hg a50s_v22_6s a50s_v22_6s_hg a50s_v23_12s a50s_v23_20s a50s_v23_6s a50s_v23_8s a50s_v23c_12s a50s_v23c_8s adv200 adv500 axiom basic basic_025 basic_035 basic_035_no_limits classic classic_no_limits classicp classicp_no_limits duet duet_no_limits duet_xs100 duet_xs100_no_limits duet_xs60 duet_xs60_no_limits edu edu_no_limits example flipsky_110_100 flipsky_110_100_no_limits flipsky_110_200 flipsky_110_200_no_limits flipsky_110_300 flipsky_110_300_no_limits flipsky_110_400 flipsky_110_400_no_limits flipsky_412 flipsky_412_no_limits flipsky_60 flipsky_60_mk5 flipsky_60_mk5_no_limits flipsky_60_no_limits flipsky_75 flipsky_75_450 flipsky_75_450_no_limits flipsky_75_no_limits fsesc_75_200_alu fsesc_75_200_alu_no_limits fsesc_75_300 gesc go_foc_dv6_pro go_foc_dv6_pro_no_limits go_foc_g300 go_foc_g300_no_limits go_foc_hi200 go_foc_hi200_no_limits go_foc_hv200 go_foc_hv200_no_limits go_foc_m100 go_foc_m100_no_limits gp hd60 hd60_no_limits hd75 hd75_no_limits itr_x1 ka160 luna_bbshd luna_m600 luna_m600_Rev5 luna_m600_Rev5_60V maxim_120 maxim_120_no_limits maxim_120_ph maxim_120_ph_no_limits maxim_150 maxim_150_no_limits maxim_150_ph maxim_150_ph_no_limits maximp_120 maximp_120_no_limits maximp_120_ph maximp_120_ph_no_limits maximp_150 maximp_150_no_limits maximp_150_ph maximp_150_ph_no_limits mbot minim minim_no_limits minim_w60 mksesc_100_300_hp mksesc_100_300_hp_no_limits mksesc_75_100_old mksesc_75_100_old_no_limits mksesc_75_100_v2 mksesc_75_100_v2_no_limits mksesc_75_100_v2_old mksesc_75_100_v2_old_no_limits mksesc_75_200_v2 mksesc_75_200_v2_no_limits mksesc_75_200_v2_old mksesc_75_200_v2_old_no_limits mksesc_84_100_hp mksesc_84_100_hp_no_limits mksesc_84_200_hp mksesc_84_200_hp_no_limits pronto pronto_no_limits r2 raiden7 rb solo solo_no_limits sparkf spesc stormcore_100d stormcore_100d_no_limits stormcore_100d_parallel stormcore_100d_v2 stormcore_100d_v2_no_limits stormcore_100dx stormcore_100dx_no_limits stormcore_100dx_parallel stormcore_100s stormcore_100s_no_limits stormcore_60d stormcore_60d+ stormcore_60d+_no_limits stormcore_60d_no_limits stormcore_60dxs stormcore_60dxs_no_limits str365 str365_150 str365_150_no_limits str365_no_limits str500 str500_01 str500_hp str500_hp_no_limits str500_no_limits ubox_126_100 ubox_126_100_no_limits ubox_126_160 ubox_126_160_no_limits ubox_single_100 ubox_single_100_no_limits ubox_single_75 ubox_single_75_no_limits ubox_single_80 ubox_single_80_no_limits ubox_single_85_200 ubox_single_85_200_no_limits ubox_v1_75_micro ubox_v1_75_micro_no_limits ubox_v1_75_typec ubox_v1_75_typec_no_limits ubox_v2_100 ubox_v2_100_no_limits ubox_v2_75 ubox_v2_75_no_limits unity unity_no_limits warrior6 x12_pro24 x12_pro30
     fw_<board>           - Build firmware for target <board>
     PROJECT=<target> fw  - Build firmware for <target>
     fw_<board>_clean     - Remove firmware for <board>
     fw_<board>_flash     - Use OpenOCD + SWD/JTAG to write firmware to <target>

     fw_custom            - Build firmware with custom hwconf file locations, you must specify these by setting the HW_SRC and HW_HEADER variables
     fw_custom_clean      - Remove firmware for custom
     fw_custom_flash      - Use OpenOCD + SWD/JTAG to write firmware to custom

   Hint: Add V=1 to your command line to see verbose build output.

   Note: All tools will be installed into /home/lee/bldc/tools
         All build output will be placed in /home/lee/bldc/build

lee@DESKTOP-QG45PR9:~/bldc$ make basic
**WARNING** ARM-SDK not in /home/lee/bldc/tools/gcc-arm-none-eabi-7-2018-q2-update  Please run 'make arm_sdk_install'
********* BUILD: basic **********
make[1]: Entering directory '/home/lee/bldc'
Compiling crt0_v7m.s
Compiling chcoreasm_v7m.s
Compiling crt1.c
Compiling vectors.c
Compiling chsys.c
Compiling chdebug.c
Compiling chvt.c
Compiling chschd.c
Compiling chthreads.c
Compiling chtm.c
Compiling chdynamic.c
Compiling chregistry.c
Compiling chsem.c
Compiling chmtx.c
Compiling chcond.c
Compiling chevents.c
Compiling chmsg.c
Compiling chmboxes.c
Compiling chqueues.c
Compiling chmemcore.c
Compiling chheap.c
Compiling chmempools.c
Compiling chcore.c
Compiling chcore_v7m.c
In file included from ChibiOS_3.0.5/os/ext/CMSIS/ST/stm32f407xx.h:184,
                 from ChibiOS_3.0.5/os/ext/CMSIS/ST/stm32f4xx.h:156,
                 from ChibiOS_3.0.5/os/common/ports/ARMCMx/devices/STM32F4xx/cmparams.h:75,
                 from ChibiOS_3.0.5/os/rt/ports/ARMCMx/chcore.h:70,
                 from ChibiOS_3.0.5/os/rt/include/ch.h:74,
                 from ChibiOS_3.0.5/os/rt/ports/ARMCMx/chcore_v7m.c:28:
ChibiOS_3.0.5/os/ext/CMSIS/include/core_cmFunc.h: In function 'PendSV_Handler':
ChibiOS_3.0.5/os/ext/CMSIS/include/core_cm4.h:94:28: warning: listing the stack pointer register 'sp' in a clobber list is deprecated [-Wdeprecated]
   94 |   #define __ASM            __asm                                      /*!< asm keyword for GNU Compiler          */
      |                            ^~~~~
ChibiOS_3.0.5/os/ext/CMSIS/include/core_cmFunc.h:441:3: note: in expansion of macro '__ASM'
  441 |   __ASM volatile ("MSR psp, %0\n" : : "r" (topOfProcStack) : "sp");
      |   ^~~~~
ChibiOS_3.0.5/os/ext/CMSIS/include/core_cm4.h:94:28: note: the value of the stack pointer after an 'asm' statement must be the same as it was before the statement
   94 |   #define __ASM            __asm                                      /*!< asm keyword for GNU Compiler          */
      |                            ^~~~~
ChibiOS_3.0.5/os/ext/CMSIS/include/core_cmFunc.h:441:3: note: in expansion of macro '__ASM'
  441 |   __ASM volatile ("MSR psp, %0\n" : : "r" (topOfProcStack) : "sp");
      |   ^~~~~
ChibiOS_3.0.5/os/ext/CMSIS/include/core_cmFunc.h: In function '_port_irq_epilogue':
ChibiOS_3.0.5/os/ext/CMSIS/include/core_cm4.h:94:28: warning: listing the stack pointer register 'sp' in a clobber list is deprecated [-Wdeprecated]
   94 |   #define __ASM            __asm                                      /*!< asm keyword for GNU Compiler          */
      |                            ^~~~~
ChibiOS_3.0.5/os/ext/CMSIS/include/core_cmFunc.h:441:3: note: in expansion of macro '__ASM'
  441 |   __ASM volatile ("MSR psp, %0\n" : : "r" (topOfProcStack) : "sp");
      |   ^~~~~
ChibiOS_3.0.5/os/ext/CMSIS/include/core_cm4.h:94:28: note: the value of the stack pointer after an 'asm' statement must be the same as it was before the statement
   94 |   #define __ASM            __asm                                      /*!< asm keyword for GNU Compiler          */
      |                            ^~~~~
ChibiOS_3.0.5/os/ext/CMSIS/include/core_cmFunc.h:441:3: note: in expansion of macro '__ASM'
  441 |   __ASM volatile ("MSR psp, %0\n" : : "r" (topOfProcStack) : "sp");
      |   ^~~~~
Compiling osal.c
Compiling hal.c
Compiling st.c
Compiling hal_queues.c
Compiling hal_mmcsd.c
Compiling can.c
Compiling i2c.c
Compiling icu.c
Compiling pal.c
Compiling serial.c
Compiling serial_usb.c
Compiling spi.c
Compiling usb.c
Compiling nvic.c
Compiling stm32_dma.c
Compiling hal_lld.c
Compiling st_lld.c
Compiling can_lld.c
Compiling pal_lld.c
Compiling i2c_lld.c
Compiling usb_lld.c
ChibiOS_3.0.5/os/hal/ports/STM32/LLD/OTGv1/usb_lld.c: In function 'otg_do_push':
ChibiOS_3.0.5/os/hal/ports/STM32/LLD/OTGv1/usb_lld.c:215:5: warning: 'packed' attribute ignored for type 'uint32_t *' {aka 'long unsigned int *'} [-Wattributes]
  215 |     *fifop = *((PACKED_VAR uint32_t *)buf);
      |     ^
ChibiOS_3.0.5/os/hal/ports/STM32/LLD/OTGv1/usb_lld.c: In function 'otg_do_pop':
ChibiOS_3.0.5/os/hal/ports/STM32/LLD/OTGv1/usb_lld.c:313:5: warning: 'packed' attribute ignored for type 'uint32_t *' {aka 'long unsigned int *'} [-Wattributes]
  313 |     *((PACKED_VAR uint32_t *)buf) = w;
      |     ^
ChibiOS_3.0.5/os/hal/ports/STM32/LLD/OTGv1/usb_lld.c: In function 'otg_fifo_read_to_buffer':
ChibiOS_3.0.5/os/hal/ports/STM32/LLD/OTGv1/usb_lld.c:342:7: warning: 'packed' attribute ignored for type 'uint32_t *' {aka 'long unsigned int *'} [-Wattributes]
  342 |       *((PACKED_VAR uint32_t *)buf) = w;
      |       ^
Compiling spi_lld.c
Compiling icu_lld.c
Compiling serial_lld.c
Compiling chprintf.c
Compiling syscalls.c
Compiling main.c
Compiling irq_handlers.c
Compiling terminal.c
Compiling conf_general.c
Compiling timeout.c
Compiling flash_helper.c
In file included from ChibiOS_3.0.5/os/ext/CMSIS/ST/stm32f407xx.h:184,
                 from ChibiOS_3.0.5/os/ext/CMSIS/ST/stm32f4xx.h:156,
                 from ChibiOS_3.0.5/os/common/ports/ARMCMx/devices/STM32F4xx/cmparams.h:75,
                 from ChibiOS_3.0.5/os/rt/ports/ARMCMx/chcore.h:70,
                 from ChibiOS_3.0.5/os/rt/include/ch.h:74,
                 from datatypes.h:25,
                 from conf_general.h:29,
                 from flash_helper.h:23,
                 from flash_helper.c:22:
ChibiOS_3.0.5/os/ext/CMSIS/include/core_cmFunc.h: In function 'flash_helper_jump_to_bootloader':
ChibiOS_3.0.5/os/ext/CMSIS/include/core_cm4.h:94:28: warning: listing the stack pointer register 'sp' in a clobber list is deprecated [-Wdeprecated]
   94 |   #define __ASM            __asm                                      /*!< asm keyword for GNU Compiler          */
      |                            ^~~~~
ChibiOS_3.0.5/os/ext/CMSIS/include/core_cmFunc.h:468:3: note: in expansion of macro '__ASM'
  468 |   __ASM volatile ("MSR msp, %0\n" : : "r" (topOfMainStack) : "sp");
      |   ^~~~~
ChibiOS_3.0.5/os/ext/CMSIS/include/core_cm4.h:94:28: note: the value of the stack pointer after an 'asm' statement must be the same as it was before the statement
   94 |   #define __ASM            __asm                                      /*!< asm keyword for GNU Compiler          */
      |                            ^~~~~
ChibiOS_3.0.5/os/ext/CMSIS/include/core_cmFunc.h:468:3: note: in expansion of macro '__ASM'
  468 |   __ASM volatile ("MSR msp, %0\n" : : "r" (topOfMainStack) : "sp");
      |   ^~~~~
Compiling confgenerator.c
Compiling bms.c
Compiling events.c
Compiling board.c
Compiling hw.c
Compiling drv8301.c
Compiling drv8305.c
Compiling drv8316.c
Compiling drv8320s.c
Compiling drv8323s.c
Compiling luna_display_serial.c
Compiling luna_m600_display.c
Compiling si8900.c
Compiling shutdown.c
Compiling app.c
Compiling app_ppm.c
Compiling app_adc.c
Compiling app_sten.c
Compiling app_uartcomm.c
Compiling app_nunchuk.c
Compiling app_pas.c
Compiling app_custom.c
Compiling canard.c
Compiling canard_driver.c
libcanard/canard_driver.c: In function 'handle_param_getset':
libcanard/canard_driver.c:894:29: warning: implicit conversion from 'enum <anonymous>' to 'uavcan_protocol_param_NumericValue_ENUM' [-Wenum-conversion]
  894 |     pkt.min_value.union_tag = UAVCAN_PROTOCOL_PARAM_VALUE_INTEGER_VALUE;
      |                             ^
libcanard/canard_driver.c:895:29: warning: implicit conversion from 'enum <anonymous>' to 'uavcan_protocol_param_NumericValue_ENUM' [-Wenum-conversion]
  895 |     pkt.max_value.union_tag = UAVCAN_PROTOCOL_PARAM_VALUE_INTEGER_VALUE;
      |                             ^
libcanard/canard_driver.c:905:29: warning: implicit conversion from 'enum <anonymous>' to 'uavcan_protocol_param_NumericValue_ENUM' [-Wenum-conversion]
  905 |     pkt.min_value.union_tag = UAVCAN_PROTOCOL_PARAM_VALUE_INTEGER_VALUE;
      |                             ^
libcanard/canard_driver.c:906:29: warning: implicit conversion from 'enum <anonymous>' to 'uavcan_protocol_param_NumericValue_ENUM' [-Wenum-conversion]
  906 |     pkt.max_value.union_tag = UAVCAN_PROTOCOL_PARAM_VALUE_INTEGER_VALUE;
      |                             ^
libcanard/canard_driver.c:916:29: warning: implicit conversion from 'enum <anonymous>' to 'uavcan_protocol_param_NumericValue_ENUM' [-Wenum-conversion]
  916 |     pkt.min_value.union_tag = UAVCAN_PROTOCOL_PARAM_VALUE_INTEGER_VALUE;
      |                             ^
libcanard/canard_driver.c:917:29: warning: implicit conversion from 'enum <anonymous>' to 'uavcan_protocol_param_NumericValue_ENUM' [-Wenum-conversion]
  917 |     pkt.max_value.union_tag = UAVCAN_PROTOCOL_PARAM_VALUE_INTEGER_VALUE;
      |                             ^
libcanard/canard_driver.c:927:29: warning: implicit conversion from 'enum <anonymous>' to 'uavcan_protocol_param_NumericValue_ENUM' [-Wenum-conversion]
  927 |     pkt.min_value.union_tag = UAVCAN_PROTOCOL_PARAM_VALUE_REAL_VALUE;
      |                             ^
libcanard/canard_driver.c:928:29: warning: implicit conversion from 'enum <anonymous>' to 'uavcan_protocol_param_NumericValue_ENUM' [-Wenum-conversion]
  928 |     pkt.max_value.union_tag = UAVCAN_PROTOCOL_PARAM_VALUE_REAL_VALUE;
      |                             ^
Compiling esc_Status.c
Compiling esc_RawCommand.c
Compiling esc_RPMCommand.c
Compiling protocol_GetNodeInfo.c
Compiling protocol_HardwareVersion.c
Compiling protocol_NodeStatus.c
Compiling protocol_SoftwareVersion.c
Compiling param_Empty.c
Compiling param_ExecuteOpcode.c
Compiling param_GetSet.c
Compiling param_NumericValue.c
Compiling param_Value.c
Compiling file_BeginFirmwareUpdate.c
Compiling file_Read.c
Compiling vesc_RTData.c
Compiling mpu9150.c
Compiling icm20948.c
Compiling ahrs.c
Compiling imu.c
Compiling bmi160.c
Compiling bmi160_wrapper.c
Compiling lsm6ds3.c
Compiling FusionAhrs.c
Compiling FusionBias.c
Compiling FusionCompass.c
Compiling bm_if.c
Compiling swdptap.c
Compiling timing.c
Compiling platform.c
Compiling exception.c
Compiling swdptap_generic.c
Compiling target.c
Compiling adiv5_swdp.c
Compiling adiv5.c
Compiling cortexm.c
Compiling nrf51.c
blackmagic/target/nrf51.c:45:17: warning: cast between incompatible function types from '_Bool (*)(target *)' {aka '_Bool (*)(struct target_s *)'} to '_Bool (*)(target *, int,  const char **)' {aka '_Bool (*)(struct target_s *, int,  const char **)'} [-Wcast-function-type]
   45 |  {"erase_mass", (cmd_handler)nrf51_cmd_erase_all, "Erase entire flash memory"},
      |                 ^
blackmagic/target/nrf51.c:46:17: warning: cast between incompatible function types from '_Bool (*)(target *)' {aka '_Bool (*)(struct target_s *)'} to '_Bool (*)(target *, int,  const char **)' {aka '_Bool (*)(struct target_s *, int,  const char **)'} [-Wcast-function-type]
   46 |  {"erase_uicr", (cmd_handler)nrf51_cmd_erase_uicr, "Erase UICR registers"},
      |                 ^
blackmagic/target/nrf51.c:51:11: warning: cast between incompatible function types from '_Bool (*)(target *)' {aka '_Bool (*)(struct target_s *)'} to '_Bool (*)(target *, int,  const char **)' {aka '_Bool (*)(struct target_s *, int,  const char **)'} [-Wcast-function-type]
   51 |  {"help", (cmd_handler)nrf51_cmd_read_help, "Display help for read commands"},
      |           ^
blackmagic/target/nrf51.c:52:11: warning: cast between incompatible function types from '_Bool (*)(target *)' {aka '_Bool (*)(struct target_s *)'} to '_Bool (*)(target *, int,  const char **)' {aka '_Bool (*)(struct target_s *, int,  const char **)'} [-Wcast-function-type]
   52 |  {"hwid", (cmd_handler)nrf51_cmd_read_hwid, "Read hardware identification number"},
      |           ^
blackmagic/target/nrf51.c:53:11: warning: cast between incompatible function types from '_Bool (*)(target *)' {aka '_Bool (*)(struct target_s *)'} to '_Bool (*)(target *, int,  const char **)' {aka '_Bool (*)(struct target_s *, int,  const char **)'} [-Wcast-function-type]
   53 |  {"fwid", (cmd_handler)nrf51_cmd_read_fwid, "Read pre-loaded firmware ID"},
      |           ^
blackmagic/target/nrf51.c:54:15: warning: cast between incompatible function types from '_Bool (*)(target *)' {aka '_Bool (*)(struct target_s *)'} to '_Bool (*)(target *, int,  const char **)' {aka '_Bool (*)(struct target_s *, int,  const char **)'} [-Wcast-function-type]
   54 |  {"deviceid", (cmd_handler)nrf51_cmd_read_deviceid, "Read unique device ID"},
      |               ^
blackmagic/target/nrf51.c:55:17: warning: cast between incompatible function types from '_Bool (*)(target *)' {aka '_Bool (*)(struct target_s *)'} to '_Bool (*)(target *, int,  const char **)' {aka '_Bool (*)(struct target_s *, int,  const char **)'} [-Wcast-function-type]
   55 |  {"deviceaddr", (cmd_handler)nrf51_cmd_read_deviceaddr, "Read device address"},
      |                 ^
blackmagic/target/nrf51.c:380:17: warning: cast between incompatible function types from '_Bool (*)(target *)' {aka '_Bool (*)(struct target_s *)'} to '_Bool (*)(target *, int,  const char **)' {aka '_Bool (*)(struct target_s *, int,  const char **)'} [-Wcast-function-type]
  380 |  {"erase_mass", (cmd_handler)nrf51_mdm_cmd_erase_mass, "Erase entire flash memory"},
      |                 ^
Compiling stm32f1.c
blackmagic/target/stm32f1.c:48:17: warning: cast between incompatible function types from '_Bool (*)(target *)' {aka '_Bool (*)(struct target_s *)'} to '_Bool (*)(target *, int,  const char **)' {aka '_Bool (*)(struct target_s *, int,  const char **)'} [-Wcast-function-type]
   48 |  {"erase_mass", (cmd_handler)stm32f1_cmd_erase_mass, "Erase entire flash memory"},
      |                 ^
Compiling stm32f4.c
blackmagic/target/stm32f4.c:47:17: warning: cast between incompatible function types from '_Bool (*)(target *)' {aka '_Bool (*)(struct target_s *)'} to '_Bool (*)(target *, int,  const char **)' {aka '_Bool (*)(struct target_s *, int,  const char **)'} [-Wcast-function-type]
   47 |  {"erase_mass", (cmd_handler)stm32f4_cmd_erase_mass,
      |                 ^
Compiling stm32h7.c
blackmagic/target/stm32h7.c:42:17: warning: cast between incompatible function types from '_Bool (*)(target *)' {aka '_Bool (*)(struct target_s *)'} to '_Bool (*)(target *, int,  const char **)' {aka '_Bool (*)(struct target_s *, int,  const char **)'} [-Wcast-function-type]
   42 |  {"erase_mass", (cmd_handler)stm32h7_cmd_erase_mass,
      |                 ^
blackmagic/target/stm32h7.c:48:10: warning: cast between incompatible function types from '_Bool (*)(target *)' {aka '_Bool (*)(struct target_s *)'} to '_Bool (*)(target *, int,  const char **)' {aka '_Bool (*)(struct target_s *, int,  const char **)'} [-Wcast-function-type]
   48 |  {"uid", (cmd_handler)stm32h7_uid, "Print unique device ID"},
      |          ^
blackmagic/target/stm32h7.c:49:10: warning: cast between incompatible function types from '_Bool (*)(target *)' {aka '_Bool (*)(struct target_s *)'} to '_Bool (*)(target *, int,  const char **)' {aka '_Bool (*)(struct target_s *, int,  const char **)'} [-Wcast-function-type]
   49 |  {"crc", (cmd_handler)stm32h7_crc, "Print CRC of both banks"},
      |          ^
Compiling stm32l0.c
Compiling stm32l4.c
blackmagic/target/stm32l4.c:48:17: warning: cast between incompatible function types from '_Bool (*)(target *)' {aka '_Bool (*)(struct target_s *)'} to '_Bool (*)(target *, int,  const char **)' {aka '_Bool (*)(struct target_s *, int,  const char **)'} [-Wcast-function-type]
   48 |  {"erase_mass", (cmd_handler)stm32l4_cmd_erase_mass, "Erase entire flash memory"},
      |                 ^
blackmagic/target/stm32l4.c:49:18: warning: cast between incompatible function types from '_Bool (*)(target *)' {aka '_Bool (*)(struct target_s *)'} to '_Bool (*)(target *, int,  const char **)' {aka '_Bool (*)(struct target_s *, int,  const char **)'} [-Wcast-function-type]
   49 |  {"erase_bank1", (cmd_handler)stm32l4_cmd_erase_bank1, "Erase entire bank1 flash memory"},
      |                  ^
blackmagic/target/stm32l4.c:50:18: warning: cast between incompatible function types from '_Bool (*)(target *)' {aka '_Bool (*)(struct target_s *)'} to '_Bool (*)(target *, int,  const char **)' {aka '_Bool (*)(struct target_s *, int,  const char **)'} [-Wcast-function-type]
   50 |  {"erase_bank2", (cmd_handler)stm32l4_cmd_erase_bank2, "Erase entire bank2 flash memory"},
      |                  ^
Compiling qmlui.c
Compiling encoder.c
Compiling encoder_cfg.c
Compiling enc_abi.c
Compiling enc_ad2s1205.c
Compiling enc_as5x47u.c
Compiling enc_as504x.c
Compiling enc_bissc.c
Compiling enc_mt6816.c
Compiling enc_sincos.c
Compiling enc_tle5012.c
Compiling enc_ts5700n8501.c
Compiling enc_pwm.c
Compiling enc_ma782.c
Compiling enc_amt22.c
Compiling conf_custom.c
Compiling env.c
Compiling fundamental.c
Compiling heap.c
Compiling lbm_memory.c
Compiling print.c
Compiling stack.c
Compiling symrepr.c
Compiling tokpar.c
Compiling extensions.c
Compiling lispbm.c
Compiling eval_cps.c
Compiling platform_mutex.c
Compiling platform_timestamp.c
Compiling lbm_channel.c
Compiling lbm_c_interop.c
Compiling lbm_custom_type.c
Compiling lbm_flat_value.c
Compiling lbm_prof.c
Compiling lbm_defrag_mem.c
Compiling lbm_image.c
Compiling array_extensions.c
Compiling math_extensions.c
Compiling string_extensions.c
Compiling mutex_extensions.c
Compiling lbm_dyn_lib.c
Compiling lispif.c
Compiling lispif_vesc_extensions.c
Compiling lispif_c_lib.c
Compiling lbm_vesc_utils.c
Compiling commands.c
Compiling comm_usb_serial.c
Compiling comm_usb.c
Compiling comm_can.c
Compiling packet.c
Compiling log.c
Compiling foc_math.c
Compiling mc_interface.c
motor/mc_interface.c: In function 'update_stats':
motor/mc_interface.c:2774:30: warning: absolute value function 'fabsf' given an argument of type 'double' but has parameter of type 'float' which may cause truncation of value [-Wabsolute-value]
 2774 |   motor->m_stats.max_speed = fabsf(speed);
      |                              ^~~~~
Compiling mcpwm.c
Compiling mcpwm_foc.c
Compiling virtual_motor.c
Compiling buffer.c
Compiling crc.c
Compiling digital_filter.c
Compiling mempools.c
Compiling utils_math.c
Compiling utils_sys.c
Compiling worker.c
Compiling minilzo.c
Compiling eeprom.c
Compiling i2c_bb.c
Compiling ledpwm.c
Compiling servo_dec.c
Compiling spi_bb.c
Compiling timer.c
Compiling pwm_servo.c
Compiling SX1278.c
Compiling lora.c
Compiling SX1278_hw.c
Compiling spi_sw.c
Compiling rf.c
Compiling rfhelp.c
Compiling nrf_driver.c
Compiling misc.c
Compiling stm32f4xx_adc.c
Compiling stm32f4xx_dma.c
Compiling stm32f4xx_exti.c
Compiling stm32f4xx_flash.c
Compiling stm32f4xx_rcc.c
Compiling stm32f4xx_syscfg.c
Compiling stm32f4xx_tim.c
Compiling stm32f4xx_iwdg.c
Compiling stm32f4xx_wwdg.c
Linking /home/lee/bldc/build/basic/basic.elf
Memory region         Used Size  Region Size  %age Used
           flash:       15684 B        16 KB     95.73%
          flash2:      464712 B     475120 B     97.81%
         crcinfo:           8 B          8 B    100.00%
            ram0:      109064 B       128 KB     83.21%
            ram1:          0 GB       112 KB      0.00%
            ram2:          0 GB        16 KB      0.00%
            ram3:          0 GB         0 GB
            ram4:       63208 B        62 KB     99.56%
           libif:          2 KB         2 KB    100.00%
            ram5:          0 GB         4 KB      0.00%
            ram6:          0 GB         0 GB
            ram7:          0 GB         0 GB
Creating /home/lee/bldc/build/basic/basic.hex
Creating /home/lee/bldc/build/basic/basic.bin
Creating /home/lee/bldc/build/basic/basic.dmp

   text    data     bss     dec     hex filename
 477618    2784  171984  652386   9f462 /home/lee/bldc/build/basic/basic.elf
Creating /home/lee/bldc/build/basic/basic.list

Done
make[1]: Leaving directory '/home/lee/bldc'
lee@DESKTOP-QG45PR9:~/bldc$ ls -lh build/basic/
total 18M
-rwxr-xr-x 1 lee lee 512K Jun  2 13:44 basic.bin
-rw-r--r-- 1 lee lee 200K Jun  2 13:44 basic.dmp
-rwxr-xr-x 1 lee lee 4.7M Jun  2 13:44 basic.elf
-rw-r--r-- 1 lee lee 1.3M Jun  2 13:44 basic.hex
-rw-r--r-- 1 lee lee 8.9M Jun  2 13:44 basic.list
-rw-r--r-- 1 lee lee 1.9M Jun  2 13:44 basic.map
drwxr-xr-x 2 lee lee  12K Jun  2 13:44 lst
drwxr-xr-x 2 lee lee  12K Jun  2 13:44 obj
lee@DESKTOP-QG45PR9:~/bldc$ grep -rn "basic" Makefile makefiles/ 2>/dev/null
lee@DESKTOP-QG45PR9:~/bldc$ find hwconf/ -iname "*basic*"
hwconf/vesc/basic
hwconf/vesc/basic/hw_basic_core.c
hwconf/vesc/basic/hw_basic_035.h
hwconf/vesc/basic/hw_basic_core.h
hwconf/vesc/basic/hw_basic.h
hwconf/vesc/basic/hw_basic_035_no_limits.h
hwconf/vesc/basic/hw_basic_025.h
lee@DESKTOP-QG45PR9:~/bldc$
```  
