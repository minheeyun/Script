#include "pwm.h"
/* 차속 기반 PWM 듀티 계산 */
uint16 Pwm_CalcDuty(uint16 speed, boolean fault)
{
    if (fault) {
        return 0u;
    }
    if (speed > 30u) {
        return 800u;
    }
    return (uint16)(speed * 20u);
}
