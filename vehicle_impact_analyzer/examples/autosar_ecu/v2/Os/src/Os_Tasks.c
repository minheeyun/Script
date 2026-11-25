#include "spd.h"
#include "pwm.h"
#include "diag.h"
/* OS 태스크(RTE가 Runnable을 순서대로 호출) */
void Task_10ms(void)
{
    Spd_MainFunction();
    Diag_MainFunction();
    Pwm_MainFunction();
}
