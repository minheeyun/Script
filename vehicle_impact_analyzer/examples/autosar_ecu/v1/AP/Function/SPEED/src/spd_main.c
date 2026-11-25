#include "spd.h"
#include "util.h"
#include "Rte_Lss.h"
uint16 g_vehicle_speed;
static uint16 s_prev;
/* 차속 필터 */
uint16 Spd_Filter(uint16 raw)
{
    s_prev = (uint16)((s_prev * 3u + raw) / 4u);
    return s_prev;
}
/* Runnable RE_SpdMain(10ms) */
void Spd_MainFunction(void)
{
    uint16 raw = 0u;
    (void)Rte_Read_RpSpeedRaw_Value(&raw);
    g_vehicle_speed = Util_Clamp(Spd_Filter(raw), 0u, SPD_MAX_KPH);
}
