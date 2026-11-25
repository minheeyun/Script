#include "util.h"
/* 공용: 값 제한 */
uint16 Util_Clamp(uint16 v, uint16 lo, uint16 hi)
{
    if (v < lo) {
        return lo;
    }
    if (v >= hi) {
        return hi;
    }
    return v;
}
