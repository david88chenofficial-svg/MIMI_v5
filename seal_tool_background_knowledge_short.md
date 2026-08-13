# Background Knowledge for Developing the Labyrinth-Seal Tool

## 1. What the tool should model

For the geometry in the sketch, the correct first reduced-order model is a **quasi-1D, cavity-by-cavity compressible labyrinth-seal model**:

- each **tooth** is a throttling restriction;
- each **cavity** between teeth is a control volume with one representative static pressure;
- the same steady **mass flow rate** passes through every tooth;
- the solver finds the unknown **cavity pressures** and total **leakage mass flow**.

This is the minimum model that can predict both:

1. total leakage, and  
2. axial pressure distribution along the rotor surface.

A single lumped leakage formula is not enough, because it gives only total flow and not the pressure in each cavity.

---

## 2. Basic seal physics

A labyrinth seal reduces leakage by forcing the flow through repeated stages of:

**restriction -> jet -> cavity mixing/dissipation -> next restriction**.

At each tooth:
- the flow accelerates through the small clearance;
- static pressure drops;
- a high-speed jet enters the downstream cavity.

Inside the cavity:
- some jet kinetic energy is lost by mixing, separation, and recirculation;
- the next tooth then throttles the flow again.

### Why clearance matters most

For an annular tooth, the throat area is approximately

\[
A_i = \pi D_i c_i
\]

where:
- \(D_i\) = tooth diameter,
- \(c_i\) = tooth-tip clearance.

So leakage is highly sensitive to clearance because the available flow area is directly proportional to \(c_i\).

### Why tooth pitch matters

If the pitch is too small, the jet from one tooth is not fully dissipated before reaching the next tooth. This is called **carryover**. More carryover means:

- less pressure recovery in the cavity,
- larger effective driving energy into the next tooth,
- higher leakage.

So pitch is not just a layout dimension; it changes the physics of stage-to-stage dissipation.

---

## 3. Geometry that actually enters the model

The tool only needs geometry that affects the flow equations.

### Must-have geometry

- **Rotor diameter** \(D_i\) at each tooth  
  Enters directly into the throat area \(A_i = \pi D_i c_i\).

- **Tooth-tip clearance** \(c_i\)  
  Main leakage-control variable.

- **Tooth pitch** \(l_{\text{pitch},i}\)  
  Affects carryover.

- **Tooth tip width / land width** \(t_i\)  
  Affects discharge coefficient \(C_d\).

- **Tooth depth** \(h_i\)  
  Affects cavity expansion and dissipation quality.

- **Axial cavity widths** \(s_i\)  
  Needed to map cavity pressures into rotor-surface pressure distribution \(p(x)\).

- **Tooth count and order**  
  The solver works tooth by tooth, so teeth and cavities must be indexed in axial order.

### Strong recommendation

Write the geometry as arrays:

- teeth: \(i = 1,2,\dots,N\)
- cavity pressures: \(P_1, P_2, \dots, P_{N+1}\)
- tooth clearances: \(c_i\)
- tooth areas: \(A_i\)
- cavity axial spans: \([x_{i,\text{start}}, x_{i,\text{end}}]\)

This immediately supports both uniform-clearance and progressive-clearance seals.

---

## 4. Core equations for the tool

## 4.1 Per-tooth leakage equation

The most useful stage equation for the first solver is the compressible restriction relation used in the progressive-clearance literature:

\[
\dot m
= K_i A_i
\sqrt{ \frac{2\gamma}{\gamma-1} \frac{P_i^2}{R T_i}
\left[
\left(\frac{P_{i+1}}{P_i}\right)^{2/\gamma}
-
\left(\frac{P_{i+1}}{P_i}\right)^{(\gamma+1)/\gamma}
\right] }
\]

where:
- \(\dot m\) = seal leakage mass flow rate,
- \(P_i\) = upstream cavity pressure for tooth \(i\),
- \(P_{i+1}\) = downstream cavity pressure,
- \(T_i\) = upstream stage temperature,
- \(\gamma\) = specific heat ratio,
- \(R\) = gas constant,
- \(A_i = \pi D_i c_i\),
- \(K_i\) = effective flow coefficient.

This is the key equation because it connects:
- tooth geometry,
- adjacent cavity pressures,
- and leakage through each stage.

## 4.2 Choking condition

A tooth chokes when the throat Mach number reaches 1. The critical pressure ratio is

\[
\left(\frac{P_{i+1}}{P_i}\right)_{\text{crit}}
=
\left(\frac{2}{\gamma+1}\right)^{\gamma/(\gamma-1)}
\]

If the calculated ratio falls below this value, that stage is choked and the mass flow should be limited by the upstream state.

A useful choked-flow form is

\[
\dot m_{\text{choked},i}
=
K_i A_i P_i
\sqrt{\frac{\gamma}{R T_i}}
\left(\frac{2}{\gamma+1}\right)^{\frac{\gamma+1}{2(\gamma-1)}}
\]

### Why choking matters

Once a stage chokes, reducing downstream pressure further does not increase the flow through that stage. The upstream cavity pressures must then adjust so the same \(\dot m\) still passes through all teeth.

---

## 5. Discharge coefficient and flow coefficient

The discharge coefficient is

\[
C_d = \frac{\dot m_{\text{actual}}}{\dot m_{\text{ideal}}}
\]

It accounts for contraction and losses at the tooth tip. Physically, \(C_d < 1\) because:
- the flow separates at sharp edges,
- the effective jet area is smaller than the geometric throat area,
- turbulence and mixing reduce the actual flow relative to the ideal isentropic value.

### What changes \(C_d\)

\(C_d\) is affected by:
- tooth width,
- tooth profile sharpness,
- clearance,
- local chamber shape.

So two seals with the same \(D\) and \(c\) can still leak differently if the tooth geometry differs.

### What to do in the tool

For the MVP, use either:

\[
K_i = C_d = \text{constant}
\]

or a simple empirical \(C_d\) correlation if later calibration data are available.

---

## 6. Carryover factor

Carryover means some kinetic energy from one tooth survives into the next cavity instead of being fully dissipated.

### Effect on performance

More carryover causes:
- less static-pressure recovery in the cavity,
- stronger approach flow into the next tooth,
- higher leakage than a fully dissipated cavity model would predict.

This becomes more important when:
- pitch is small,
- clearance is large relative to pitch,
- cavity dissipation is weak.

### Modelling guidance

For a first solver, carryover can be handled in one of two ways:

1. absorb it into an effective \(K_i\) or global calibration factor;  
2. add an explicit carryover correction later once the base solver works.

For the MVP, the main point is: **ignoring carryover usually makes leakage look too low** when cavities are not effective diffusers.

---

## 7. Unknowns and how to solve them

For a seal with \(N\) teeth, define:

- \(P_1 = p_{\text{in}}\)
- \(P_{N+1} = p_{\text{out}}\)
- unknown intermediate cavity pressures: \(P_2, \dots, P_N\)
- one unknown leakage rate: \(\dot m\)

Each tooth gives one equation:

\[
\dot m = f_i(P_i, P_{i+1})
\]

So the full problem is closed.

### Best numerical strategy for the MVP

Do **not** solve the whole system at once first. Use a 1D root-find on mass flow:

1. guess \(\dot m\),
2. start from \(P_1 = p_{\text{in}}\),
3. solve each tooth equation for \(P_{i+1}\), marching downstream,
4. compare the final predicted \(P_{N+1}\) with the required \(p_{\text{out}}\),
5. adjust \(\dot m\) until the outlet matches.

This is simpler and usually more robust than solving all pressures simultaneously with a large nonlinear system.

---

## 8. Temperature model for the first version

For the MVP, it is acceptable to take

\[
T_i = T_{\text{in}}
\]

for all stages.

That is not exact, but it is reasonable for a first reduced-order model because the main target is pressure distribution and leakage, not detailed thermal behaviour.

---

## 9. How to obtain rotor-surface pressure distribution

The requested pressure distribution is not a CFD field. In this model it should be interpreted as a **mapped cavity-pressure profile**.

### First-order mapping

Assign one pressure to each rotor surface segment facing a cavity:

\[
p(x) = P_i \quad \text{over cavity } i
\]

and a pressure drop across each tooth:

\[
\Delta P_i = P_i - P_{i+1}
\]

So the output pressure profile is naturally stagewise:
- nearly constant over each cavity,
- sharp drop at each tooth.

This is the correct reduced-order interpretation of rotor-surface pressure distribution.

---

## 10. Progressive-clearance seals

A uniform seal has

\[
c_1 = c_2 = \cdots = c_N
\]

A progressive-clearance seal has tooth-dependent clearances, for example

\[
c_1 > c_2 > \cdots > c_N
\]

### Why this matters

If downstream teeth are tighter:
- their flow capacity decreases,
- upstream cavity pressures rise to maintain the same \(\dot m\),
- the pressure profile shifts upward,
- the axial pressure loading on the seal increases.

So the tool should be written from the start with **per-tooth clearances \(c_i\)**, even if the first test case uses uniform clearance.

---

## 11. What the tool should output

### Primary outputs

- total leakage mass flow rate \(\dot m\)
- cavity pressures \(P_i\)
- rotor-surface pressure distribution \(p(x)\)

### Useful secondary outputs

- tooth pressure drops \(\Delta P_i\)
- choking flag for each tooth
- tooth throat areas \(A_i\)

These secondary outputs are important for debugging and for understanding which stages dominate the pressure loss.

---

## 12. Assumptions that are acceptable for the MVP

The first tool can assume:

- steady flow,
- axisymmetric geometry,
- quasi-1D flow through each tooth,
- one uniform static pressure per cavity,
- constant gas properties,
- fixed geometry with no rubbing or vibration,
- isothermal or stage-reset temperature approximation.

### What this first tool will **not** capture

- rotordynamics,
- windage heating,
- transient motion,
- circumferential non-uniformity,
- detailed CFD flow structures,
- rubbing damage.

That is acceptable. The goal is a physically meaningful engineering model, not a full CFD replacement.

---

## 13. Minimum implementation hierarchy

If someone is building the tool from scratch, the order should be:

### Must implement first

1. tooth area
   \[
   A_i = \pi D_i c_i
   \]
2. per-tooth compressible leakage equation  
3. choking check using the critical pressure ratio  
4. downstream pressure marching + 1D root-find on \(\dot m\)  
5. stagewise pressure mapping into \(p(x)\)

### Add only after the base solver works

6. better \(C_d\) model  
7. carryover correction  
8. progressive-clearance studies  
9. force integration from the pressure profile

---

## 14. Final takeaway

The first useful tool is a **nonlinear stage-by-stage compressible labyrinth-seal solver**.

It should:
- treat each tooth as a compressible restriction,
- treat each cavity as one pressure node,
- solve for one common leakage rate and all cavity pressures,
- detect choking,
- return both total leakage and stagewise rotor-surface pressure distribution.

That is the smallest model that is still technically defensible and directly useful for the geometry in the sketch.

---

## References used to extract the modelling basis

- GE progressive-clearance labyrinth seal paper  
- Classical seal textbook material on labyrinth stages, choking, discharge coefficient, and carryover  
- Kearton radial-labyrinth treatment for choking interpretation  
- Ueda material for classical labyrinth-seal modelling background  
- Cross non-contact seal paper for clearance-control and tooth-profile design insight
