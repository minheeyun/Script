#include "pwm.h"
#include "spd.h"
#include "diag.h"
#include "util.h"
#include "Pwm.h"
#include "Rte_Lss.h"
/* Runnable RE_PwmMain(10ms) */
void Pwm_MainFunction(void)
{
    uint16 duty = Pwm_CalcDuty(g_vehicle_speed, g_diag_fault);
    duty = Util_Clamp(duty, 0u, 1000u);
    Pwm_SetDutyCycle(0u, duty);
    (void)Rte_Write_PpPwmDuty_Value(duty);
}
