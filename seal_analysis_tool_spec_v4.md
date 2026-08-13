# Refined Seal Analysis Tool Specification

## Objective
Develop a reduced-order solver for an axisymmetric labyrinth seal with a fixed rotor, a floating stator and a rotor with toothed sections at the inlet and outlet. The tool should predict:

- total leakage mass flow rate, `m_dot`, for a pressure ratio of 5 bar
- pressure distribution along the rotor surface, `p(s)`
    - the flow passing the inlet teeth is radial outwards
    - the flow passing the outlet teeth is radial inwards
- total axial force acting on the floating stator `F_total`
- maintain non-dimensional groups if possible

## Model Scope
Use a cavity-by-cavity, quasi-1D compressible-flow model:

- each tooth is treated as a throttling restriction
- each cavity is treated as a control volume with approximately uniform static pressure
- a single steady mass flow rate is enforced through all stages
- intermediate inlet and outlet pressures between each tooth and cavity are to be used as boundary conditions for the next
- solved cavity pressures are mapped onto rotor-surface segments to form `p(s)`

This is a suitable MVP because it captures both leakage, pressure distribution and forces without requiring full CFD.

## Required Inputs

### Boundary conditions
- overall inlet stagnation pressure
- overall outlet static pressure

### Fluid properties
- working fluid
- inlet temperature
- specific heat ratio, `gamma`
- gas constant, `R`

### Main geometry
- inner radius or rotor diameter
- outer radius
- floating stator inlet diameter
- floating stator outlet diameter
- floating stator radial clearance

### Seal-gap geometry
- nominal stator-rotor gap
- tooth-tip clearance, `c`

### Tooth geometry
- tooth tip thickness
- tooth depth
- tooth pitch
- tooth disengagement gap

### Tooth layout
- number of inlet-side teeth
- number of outlet-side teeth
- axial spacing between teeth

### Axial placement
The solver must know the axial positions of the inlet tooth set, central rotor section, outlet tooth set, and cavity boundaries. In the first version, these can be generated automatically from tooth counts and geometric inputs.

## Recommended Additional Inputs
- discharge coefficient for a tooth, `Cd`
- carryover factor, `k`
- choked-flow check on/off
- constant or correlation-based `Cd`
- constant or correlation-based carryover
- number of surface points for `p(s)`

## Outputs

### Primary outputs
- mass flow rate, `m_dot`
- rotor-surface pressure distribution, `p(s)`
- force acting on the rotor due to the pressure difference, `F_total`

### Secondary outputs
- cavity pressures
- pressure drop across each tooth
- choked or unchoked status of each stage
- local effective flow area at each restriction
- interpreted geometry summary

## Pressure Distribution Definition
For the MVP, `p(s)` should be the mapped cavity-pressure distribution:

- each rotor segment facing a cavity is assigned a nearly uniform pressure
- pressure drops occur across teeth from mixing of the jet at tooth outlet
- the result is a piecewise or piecewise-smoothed pressure profile

## Minimal MVP Input Set
- inlet static pressure
- outlet static pressure
- inlet temperature
- `gamma`
- `R`
- nominal stator-rotor gap
- tooth-tip clearance
- inner radius
- outer radius
- tooth tip thickness
- tooth depth
- tooth pitch
- tooth disengagement gap
- number of inlet-side teeth
- number of outlet-side teeth

## Future Extensions
- variable tooth clearances
- nonuniform tooth pitch
- stage-dependent flow area
- calibration of `Cd` and carryover using CFD or experiments
- fluid-force prediction from `p(s)`
- transient or eccentric-rotor effects

## Summary
The tool should be a reduced-order labyrinth-seal solver that uses boundary pressures, fluid properties, and seal geometry to predict leakage mass flow rate and rotor-surface pressure distribution. It should support symmetric or asymmetric tooth layouts while remaining physically predictive and simple enough for a first implementation.