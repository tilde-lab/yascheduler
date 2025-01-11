#!/usr/bin/env python3
"""Submit FP task"""

from yascheduler import Yascheduler

LABEL = "Al2O3 XRPD pattern"

PATTERN_REQUEST = """COMM Corindon Al2O3
! Files => DAT-file: corindon, PCR-file: corindonx
!Job Npr Nph Nba Nex Nsc Nor Dum Iwg Ilo Ias Res Ste Nre Cry Uni Cor Opt Aut
 2 5 1 0 0 0 1 0 0 0 0 0 0 0 0 0 0 0 0
!
!Ipr Ppl Ioc Mat Pcr Ls1 Ls2 Ls3 NLI Prf Ins Rpa Sym Hkl Fou Sho Ana
 0 0 1 0 1 0 0 0 0 2 0 0 0 0 0 0 1
!
! lambda1 Lambda2 Ratio Bkpos Wdt Cthm muR AsyLim Rpolarz ->Patt# 1
1.540596 1.540596 0.50 65.00 20.00 0.7998 0.00 40.00 0.0000
!
!NCY Eps R_at R_an R_pr R_gl Thmin Step Thmax PSD Sent0
 1 0.01 0.65 0.65 0.35 0.65 10.0000 0.100000 175.0000 0.000 0.000
!
 0 !Number of refined parameters
!
! Zero Code SyCos Code SySin Code Lambda Code MORE ->Patt# 1
-0.090 21.00 0.0000 0.00 0.0000 0.00 0.0000 0.0000 0
! Background coefficients/codes for Pattern# 1
21.00 -5.66 -9.36 8.30 37.04 -24.55
 51.00 61.00 71.00 81.00 31.00 41.00
!-------------------------------------------------------------------------------
! Data for PHASE number: 1
!-------------------------------------------------------------------------------
Al2O3
!
!Nat Dis Ang Pr1 Pr2 Pr3 Jbt Irf Isy Str Furth ATZ Nvk Npr More
 2 0 0 0.0 0.0 1.0 0 0 0 0 0 611.760 0 5 0
!
R -3 c <--Space group symbol
!Atom Typ X Y Z Biso Occ In Fin N_t Spc /Codes
AL AL+3 0.00000 0.00000 0.35218 0.4000 0.33333 0 0 0 0
 0.00 0. 00 171.00 191.00 0.00
O O-2 0.30610 0.00000 0.25000 0.5000 0.50000 0 0 0 0
 181.00 0.00 0.00 201.00 0.00
!-------> Profile Parameters for Pattern # 1
! Scale Shape1 Bov Str1 Str2 Str3 Strain-Model
0.28556E-02 0.5000 0.00000 0.00000 0.00000 0.00000 0
 11.00000 161.000 0.000 0.000 0.000 0.000
! U V W X Y GauSiz LorSiz Size-Model
 0.005865 0.025089 0.018587 0.000000 0.000000 0.000000 0.000000 0
 141.000 151.000 131.000 0.000 0.000 0.000 0.000
! a b c alpha beta gamma #Cell Info
 4.75758 4.75758 12.9876 90.000 90.000 120.000
 111.000 111.000 121.000 0.000 0.000 111.000
! Pref1 Pref2 Asy1 Asy2 Asy3 Asy4
 0.00000 0.00000 0.023 0.061 0.00000 0.00000
 0.00 0.00 91.00 101.00 0.00 0.00
"""

yac = Yascheduler()
result = yac.queue_submit_task(LABEL, {"calc.pcr": PATTERN_REQUEST}, "fullprof")
print(LABEL)
print(result)
