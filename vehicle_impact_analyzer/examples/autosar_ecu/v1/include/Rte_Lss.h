#ifndef RTE_LSS_H
#define RTE_LSS_H
#include "Std_Types.h"
/* RTE(생성 코드) 인터페이스: 본문은 RTE 생성기가 만든다 */
uint8 Rte_Read_RpSpeedRaw_Value(uint16 *value);
uint8 Rte_Write_PpPwmDuty_Value(uint16 value);
uint8 Rte_Write_PpDiagFault_Value(boolean value);
#endif
