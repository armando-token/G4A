# Phase 01 — Physical Domain Model & Paired Fixture Specification

## Overview
Phase 01 defines the deterministic electro-thermal physical kernel (`blackbox/domain.py`) and paired counterfactual fixture semantics for **BlackboxDC**. Every simulation trajectory, probe measurement, parameter identification step, and workload migration action is governed by closed-form discrete-time physical equations with strict input validation.

---

## 1. Typed Domain Contracts

| Type | Key Fields | Invariants & Validation |
| :--- | :--- | :--- |
| `RackParams` | `rack_id`, `pdu_id`, `current_A`, `initial_temp_C`, `R0_ohm`, `alpha_per_C`, `h_W_per_K`, `C_J_per_K`, `max_current_A`, `max_temp_C=90.0` | All floats finite; non-empty IDs; `current_A >= 0`; `R0_ohm > 0`; `alpha_per_C >= 0`; `h_W_per_K > 0`; `C_J_per_K > 0`; `max_current_A > 0`; `max_temp_C > ambient_C`. |
| `PDU` | `pdu_id`, `max_current_A` | Non-empty `pdu_id`; finite `max_current_A > 0`. |
| `Migration` | `source`, `target`, `amps=8.0` | Non-empty `source`, `target`; finite `amps > 0`. |
| `World` | `racks`, `pdus`, `ambient_C=24.0`, `horizon_s=180`, `step_s=10`, `allowed_targets=("N2", "N3")` | Unique `rack_id` and `pdu_id`; every `rack.pdu_id` in `pdus`; `horizon_s == 180`; `step_s > 0` and divides `horizon_s`; total current $\sum I_i = 68.0\text{ A}$. |
| `Trajectory` | `time_s`, `temps_C`, `currents_A`, `pdu_currents_A`, `safety_flags` | 19 time samples (`t = 0, 10, ..., 180`); per-rack temperatures and currents; per-PDU currents; structured `safety_flags` with `verdict`. |

---

## 2. Electro-Thermal Physics & Discrete Step Equation

For a rack at temperature $T_k$ ($^\circ\text{C}$) carrying load current $I$ ($\text{A}$) in ambient temperature $T_{\text{amb}}$ ($24.0^\circ\text{C}$):

1. **Temperature-Dependent Joint Resistance**:
   $$R(T_k) = R_0 \cdot \bigl(1 + \alpha \cdot \max(T_k - T_{\text{amb}}, 0)\bigr)$$
2. **Instantaneous Equilibrium Temperature**:
   $$T_{\text{eq}}(T_k, I) = T_{\text{amb}} + \frac{I^2 \cdot R(T_k)}{h}$$
3. **Exact Step Update ($\Delta t = 10\text{ s}$)**:
   $$T_{k+1} = T_{\text{eq}} + (T_k - T_{\text{eq}}) \cdot \exp\left(-\frac{h \cdot \Delta t}{C}\right)$$

---

## 3. Parameter Identifiability from Probe & Thermal Ramp

1. **Base Resistance $R_0$ from Voltage Drop Probe**:
   $$R_0 = \frac{V_{\text{drop}}}{I \cdot \bigl(1 + \alpha \cdot \max(T - T_{\text{amb}}, 0)\bigr)}$$
2. **Thermal Conductance $h$ and Heat Capacity $C$ from Ramp Least-Squares**:
   Let $\theta_k = T_k - T_{\text{amb}}$. Consecutive samples at fixed $\Delta t = 10\text{ s}$ satisfy the linear regression model:
   $$\theta_{k+1} = a \cdot \theta_k + b \cdot I_k^2 \bigl(1 + \alpha \cdot \max(\theta_k, 0)\bigr)$$
   where:
   $$h = \frac{R_0 (1 - a)}{b}, \qquad C = -\frac{h \cdot \Delta t}{\ln(a)}$$

---

## 4. Reference Operating Table (68.0 A Total Load)

| Scenario | Migration | Active PDU Feeder Load (`F1` / `F2`, limit `44.0 A`) | Thermal Check (`< 90.0 C`) | Safety Verdict |
| :--- | :--- | :--- | :--- | :--- |
| **No Remediation** | `None` (`N4` remains at `24.0 A` / high $R_0$) | Within PDU limits (`40.0 A` / `28.0 A`) | Violated on `N4` ($\ge 90.0^\circ\text{C}$) | `"unsafe, thermal"` |
| **Migrate `8.0 A` to rack on `F1`** | `Migration(source="N4", target=rack_on_F1, amps=8.0)` | `F1 = 48.0 A > 44.0 A` (**Overload**), `F2 = 20.0 A` | All racks $< 90.0^\circ\text{C}$ | `"unsafe, F1 overload"` |
| **Migrate `8.0 A` to rack on `F2`** | `Migration(source="N4", target=rack_on_F2, amps=8.0)` | `F1 = 40.0 A <= 44.0 A`, `F2 = 28.0 A <= 44.0 A` | All racks $< 90.0^\circ\text{C}$ | `"safe"` |
