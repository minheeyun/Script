#include "Pwm.h"
#define PWM0_DUTY_REG (*(volatile uint32 *)0xF0003000u)
/* MCAL PWM 드라이버 */
void Pwm_SetDutyCycle(uint8 channel, uint16 duty)
{
    if (channel == 0u) {
        PWM0_DUTY_REG = duty;
    }
}
