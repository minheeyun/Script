#include "diag.h"
#include "spd.h"
#include "Rte_Lss.h"
boolean g_diag_fault;
static uint8 s_cnt;
/* Runnable RE_DiagMain(10ms): 과속 신호가 5회 이어지면 고장 */
void Diag_MainFunction(void)
{
    if (g_vehicle_speed > 250u) {
        if (s_cnt < 5u) {
            s_cnt++;
        }
    } else {
        s_cnt = 0u;
    }
    g_diag_fault = (s_cnt >= 5u) ? TRUE : FALSE;
    (void)Rte_Write_PpDiagFault_Value(g_diag_fault);
}
