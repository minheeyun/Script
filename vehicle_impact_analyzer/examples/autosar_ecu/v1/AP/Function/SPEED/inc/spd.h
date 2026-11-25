#ifndef SPD_H
#define SPD_H
#include "Std_Types.h"
#define SPD_MAX_KPH 300u
extern uint16 g_vehicle_speed;
uint16 Spd_Filter(uint16 raw);
void Spd_MainFunction(void);
#endif
