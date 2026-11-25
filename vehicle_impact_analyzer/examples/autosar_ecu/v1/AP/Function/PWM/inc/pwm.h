#ifndef PWM_APP_H
#define PWM_APP_H
#include "Std_Types.h"
uint16 Pwm_CalcDuty(uint16 speed, boolean fault);
void Pwm_MainFunction(void);
#endif
