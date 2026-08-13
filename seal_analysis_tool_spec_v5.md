# Refined Floating-Stator Seal Analysis Tool Specification

## Objective

Develop a reduced-order solver for the floating-stator radial labyrinth seal shown in the reference configuration.

The tool should predict:

- total leakage mass flow rate, `m_dot`
- pressure distribution along the rotor surface, `p(s)`
- cavity pressures through the seal path
- inlet-side stator force contribution, `F1`
- outlet-side stator force contribution, `F2`
- balancing-chamber force, `F_balance`
- net aerodynamic force acting on the floating stator, `F_stator`
- total axial force acting on the rotor, `F_rotor`
- natural frequency of the floating-stator axial vibration mode
- tooth arrangement that gives the best force balance on the floating stator

The main design purpose is to find a tooth arrangement that gives the minimum leakage mass flow rate while also making the net aerodynamic force acting on the floating stator zero or as close to zero as practical.

---

## Reference Configuration

The seal follows the reference layout:

- the rotor is the central rotating body
- the fixed stator surrounds the floating stator
- the floating stator carries the seal teeth
- the inlet-side teeth form Stage 1
- the outlet-side teeth form Stage 2
- Stage 1 flow moves radially outwards from `P0` to `P1`
- Stage 2 flow moves radially inwards from `P1` to `P2`
- `P0` is the inlet-side pressure region
- `P1` is the upper/intermediate chamber pressure
- `P2` is the outlet-side pressure region
- the floating stator is acted on by:
  - `F1` from the inlet-side tooth region
  - `F2` from the outlet-side tooth region
  - `F_balance` from the balancing chamber

For the tooth-arrangement search, the floating stator should be treated as fixed at its nominal position. The tool should then find the inlet-side and outlet-side tooth arrangement that gives the smallest net aerodynamic force on the fixed floating stator.

---

## Model Scope

Use a cavity-by-cavity, quasi-1D compressible-flow model:

- each tooth is treated as a throttling restriction
- each cavity is treated as a control volume with approximately uniform static pressure
- one steady mass flow rate passes through the full seal path
- intermediate cavity pressures are solved sequentially through the inlet-side and outlet-side teeth
- pressure drops mainly occur across the teeth
- solved cavity pressures are mapped onto rotor-facing surfaces to form `p(s)`
- the same pressure solution is used to calculate rotor and stator forces
- the inlet-side and outlet-side tooth groups are treated as separate design variables
- the floating stator is fixed during the tooth-arrangement force-balance search
- a simple vibration model is used to estimate the floating-stator natural frequency

This is a suitable MVP because it captures leakage, pressure distribution, stator force balance, rotor force, and basic vibration behaviour without requiring full CFD.

---

## General Assumptions

- The geometry is axisymmetric.
- Flow is steady and compressible.
- The working fluid is treated as an ideal gas.
- Temperature is assumed constant in the first version.
- Tooth discharge is represented using a discharge coefficient, `Cd`.
- Choking can be checked at each tooth.
- Cavity pressure is uniform within each cavity.
- The floating stator is fixed when calculating the force balance for different tooth arrangements.
- The natural frequency calculation uses a reduced-order mass-stiffness model.
- The first design search should use explicit enumeration or grid search, not a complex optimiser.

---

## Required Inputs

### Boundary conditions

- inlet pressure, `P0`
- outlet pressure, `P2`
- whether inlet pressure is static or stagnation pressure
- operating pressure range, if available

### Fluid properties

- working fluid
- inlet temperature
- specific heat ratio, `gamma`
- gas constant, `R`

### Main geometry

- rotor radius or diameter
- floating-stator inner radius
- floating-stator outer radius
- fixed-stator reference dimensions, if available
- nominal stator-rotor clearance
- available radial design space
- available axial design space

### Tooth geometry

- tooth-tip clearance, `c`
- tooth-tip thickness
- tooth depth
- tooth pitch
- tooth disengagement gap
- minimum manufacturable tooth thickness

### Tooth layout

- number of inlet-side teeth
- number of outlet-side teeth
- allowable total tooth-count range
- allowable inlet-side tooth-count range
- allowable outlet-side tooth-count range
- axial spacing between teeth

### Balancing chamber

- balancing-chamber effective pressure area
- balancing-chamber axial size, if available
- balancing-chamber radial size, if available

### Force-balance inputs

- stator pressure-area definition
- rotor pressure-area definition
- acceptable residual stator force
- acceptable residual rotor force, if required

### Vibration inputs

- estimated floating-stator mass
- bellows axial stiffness
- support stiffness
- estimated damping level, if available
- known excitation frequencies, if available
- expected axial vibration amplitude, if available

---

## Recommended Additional Inputs

- discharge coefficient, `Cd`
- carryover factor, `k`
- choked-flow check on/off
- constant or correlation-based `Cd`
- number of points for `p(s)`
- leakage limit
- force-balance tolerance
- minimum acceptable frequency separation margin
- logging option for design iterations

---

## Outputs

### Primary outputs

- leakage mass flow rate, `m_dot`
- cavity pressures
- intermediate chamber pressure, `P1`
- rotor-surface pressure distribution, `p(s)`
- inlet-side stator force contribution, `F1`
- outlet-side stator force contribution, `F2`
- balancing-chamber force, `F_balance`
- net aerodynamic force acting on the floating stator, `F_stator`
- total axial force acting on the rotor, `F_rotor`
- selected tooth arrangement giving the minimum leakage while satisfying stator force balance as closely as practical
- residual stator force after tooth-arrangement search
- natural frequency of the floating-stator axial vibration mode

### Secondary outputs

- pressure drop across each tooth
- choked or unchoked status of each tooth
- local effective flow area at each restriction
- interpreted geometry summary
- leakage versus tooth-count trend
- stator force versus tooth arrangement trend
- rotor force versus tooth arrangement trend
- vibration and resonance-risk indication
- ranked table of candidate tooth arrangements

---

## Pressure Distribution Definition

For the MVP, `p(s)` should be the mapped cavity-pressure distribution:

- each rotor segment facing a cavity is assigned a nearly uniform pressure
- pressure drops occur mainly across teeth
- Stage 1 creates a stepwise pressure change along the radial-outward flow path from `P0` to `P1`
- Stage 2 creates a stepwise pressure change along the radial-inward flow path from `P1` to `P2`
- the result is a piecewise or piecewise-smoothed pressure profile

The pressure distribution should be used consistently for:

- leakage prediction
- rotor force calculation
- floating-stator force calculation
- tooth-arrangement comparison

---

## Floating-Stator Force Balance

The tool must calculate the net aerodynamic force acting on the floating stator, `F_stator`.

The force calculation should include:

- inlet-side contribution, `F1`
- outlet-side contribution, `F2`
- balancing-chamber contribution, `F_balance`

The basic force-balance target is:

- `F_stator ≈ 0`

For the tooth-arrangement search, this balance should be evaluated with the floating stator fixed at its nominal position.

The tool should indicate whether the stator force is:

- approximately balanced
- inlet-side biased
- outlet-side biased
- unacceptable because the residual force is too large

If exact balance is not possible, the tool should report the layout with the smallest remaining stator force among feasible low-leakage designs.

---

## Tooth Arrangement Search

After the base solver has been built, the tool should search over different tooth arrangements.

The search variables should include:

- total number of teeth
- number of inlet-side teeth
- number of outlet-side teeth
- inlet-side tooth pitch
- outlet-side tooth pitch
- tooth depth
- tooth spacing
- balancing-chamber effective area, if allowed to vary

For each candidate arrangement, the tool should calculate:

- leakage mass flow rate, `m_dot`
- cavity pressures
- `P1`
- rotor pressure distribution, `p(s)`
- rotor force, `F_rotor`
- stator force, `F_stator`
- natural frequency, if vibration inputs are available

The preferred design should have:

- minimum leakage mass flow rate among feasible candidates
- near-zero net aerodynamic force on the fixed floating stator
- acceptable pressure distribution
- acceptable rotor force
- acceptable clearance margin
- practical tooth geometry
- acceptable vibration behaviour, if checked

The design ranking should therefore prioritise low `m_dot`, while rejecting or penalising layouts that produce excessive residual `F_stator`.

The output of the search should be a ranked table of candidate arrangements, showing the trade-off between leakage, stator force balance, rotor force, and vibration risk.

---

## Natural Frequency Check

The tool should include a simple reduced-order check of the floating-stator axial vibration mode.

The natural-frequency estimate should use:

- floating-stator mass
- bellows stiffness
- support stiffness
- force-displacement behaviour, if available

The tool should compare the predicted natural frequency with known excitation frequencies, if provided.

The design should be flagged if:

- the natural frequency is too close to an excitation frequency
- the estimated stiffness is negative or unrealistic
- expected vibration amplitude may close the seal gap
- the clearance margin is insufficient

---

## Minimal MVP Input Set

- inlet pressure, `P0`
- outlet pressure, `P2`
- inlet temperature
- `gamma`
- `R`
- rotor radius or diameter
- floating-stator inner radius
- floating-stator outer radius
- nominal stator-rotor clearance
- tooth-tip clearance
- tooth-tip thickness
- tooth depth
- tooth pitch
- tooth disengagement gap
- number of inlet-side teeth
- number of outlet-side teeth
- allowable total tooth-count range
- balancing-chamber effective area
- discharge coefficient, `Cd`
- estimated floating-stator mass
- bellows or support stiffness

---

## Minimal MVP Outputs

- leakage mass flow rate, `m_dot`
- cavity pressures
- intermediate pressure, `P1`
- rotor-surface pressure distribution, `p(s)`
- inlet-side stator force, `F1`
- outlet-side stator force, `F2`
- balancing force, `F_balance`
- total floating-stator force, `F_stator`
- total rotor axial force, `F_rotor`
- best inlet/outlet tooth arrangement from the search
- residual stator force imbalance
- statement of whether the floating stator is aerodynamically balanced
- floating-stator natural frequency
- basic resonance-risk warning, if excitation inputs are available

---

## Future Extensions

- variable tooth clearances
- nonuniform tooth pitch
- stage-dependent discharge coefficient
- calibration of `Cd` and carryover using CFD or experiments
- improved pressure-area model for stator force prediction
- floating-stator displacement effects during force-balance search
- transient or eccentric-rotor effects
- coupled rotor-floating-stator dynamics
- detailed floating-stator and bellows structural model
- automatic CAD or technical drawing generation

---

## Summary

The tool should be a reduced-order floating-stator labyrinth-seal solver that uses boundary pressures, fluid properties, and seal geometry to predict leakage mass flow rate, cavity pressures, rotor-surface pressure distribution, rotor force, and floating-stator force.

The inlet-side and outlet-side teeth should be treated as two separate design groups. After the base solver is built, the tool should search over tooth arrangements to find the layout that gives the minimum leakage mass flow rate while making the net aerodynamic force on the fixed floating stator zero or as close to zero as practical.

The main design target is:

- minimum feasible leakage
- near-zero net aerodynamic force on the fixed floating stator
- acceptable rotor force
- acceptable pressure distribution
- acceptable natural frequency and vibration risk