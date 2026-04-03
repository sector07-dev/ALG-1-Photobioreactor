# ALG-1 Photobioreactor

**Codex Entry: Recovered Prototype**

---

## ARCHIVAL STATUS

**Recovered. Partial reconstruction complete.**
Compiled by **ARI — Analysis & Reconstruction Intelligence**

---

## OVERVIEW

This repository contains the reconstructed design, firmware, and operational framework for the **ALG-1 Photobioreactor** — a controlled environment system engineered to cultivate edible microalgae.

Historical analysis indicates that this device was developed to simulate and regulate the critical variables required for sustained algae growth:

* Light intensity and cycle
* Temperature regulation
* pH monitoring and sampling
* Fluid movement and aeration

Unlike natural systems, ALG-1 operates as a **fully enclosed artificial ecosystem**, decoupled from external environmental instability.

It does not rely on sunlight.
It creates its own version of it.

---

## PURPOSE

Recovered intent suggests three primary objectives:

1. **Food Production**
   Cultivation of nutrient-dense microalgae (e.g., spirulina) as a scalable food source for fish in an aquaponic system.

2. **System Automation**
   Demonstration of a self-regulating biological environment using embedded systems.

3. **Open Replication**
   Design structured for reproducibility by external operators.

---

## SYSTEM ARCHITECTURE

### Core Components

* **Controller Layer**

  * Raspberry Pi 5(System Manager + UI)
  * Dual microcontroller nodes:

    * **A1** — pH Sampling Module
    * **A2** — Environmental Control Module

* **Environmental Systems**

  * LED lighting array (top + bottom illumination)
  * Heating system with scheduled cycles
  * Air injection system for mixing and gas exchange

* **Fluid Handling**

  * Pump-based sampling system
  * Automated wash, sampling, and calibration using pH sensor.

* **Sensing**

  * pH probe (calibrated)
  * Temperature monitoring
  * Indirect density estimation (light attenuation)

---

## SOFTWARE

### Interface

* Python-based GUI (touchscreen optimized)
* Real-time telemetry visualization
* Manual and automated control modes
* Fault-tolerant serial communication layer

### Features

* Subsystem toggles (modular operation)
* Automation scheduling (day/night cycles)
* Manual overrides for testing and intervention
* Device reconnection logic for system resilience

---

## OPERATIONAL MODEL

The reactor maintains a dynamic equilibrium through controlled cycles:

1. **Day Phase**

   * Increased temperature
   * Active illumination
   * Accelerated growth conditions

2. **Night Phase**

   * Reduced temperature
   * Light disabled
   * Stabilization period

3. **pH Evolution**

   * Natural increase over time as culture matures
   * Used as an indicator for harvest readiness

---

## BUILD PHILOSOPHY

Analysis of design patterns reveals the following principles:

* **Modularity over integration**
  Systems are separable, replaceable, and independently testable.

* **Failure tolerance**
  Communication loss does not immediately collapse the system.

* **Accessibility**
  Components selected for availability and affordability.

* **Transparency**
  System behavior is observable and adjustable.

---

## REPLICATION

This system was intended to be built but has known issues and shortcomings in the design.

All required information is provided, including:

* Firmware for microcontrollers
* GUI Control software
* Mechanical print files
* Component list/bill of materials.

Operators are expected to use this prototype for reference only. It is not recommended to rebuild in its current state.

---

## KNOWN LIMITATIONS

* Automated pH balancing and nutrient addition features are unused. Resevoirs for these are unused. Different solution required.
* Manual nutrient addition (not automated)
* pH sampler drain /fill cannot be trusted. Algae mats can clog drain resulting in overfill (shorting hazard)
* pH sampler uses stepper motors and hall effect sensors as closed loop feedback. Cost can be reduced by using DC gearmotors + sensors instead.
* Pump/power supply enclosure requires redesign for better heat dissipation. Air pump ceased to function after two months continous operation. 
* GUI Dashboard meters need rework
* A2 Firmware needs the dispense algae routine to be fixed.

This is not a complete design. Issues persist.
It is a controlled living system.

---

## SAFETY NOTES

* Do not replicate this project exactly as defined. You may build upon or rework the system using your own experience to correct issues.
* Maintain proper electrical isolation between voltage domains.
* Design currently lacks fuses for safety (prototype).

---

## FINAL ANALYSIS

The ALG-1 Photobioreactor represents a convergence of:

* Embedded systems
* Environmental control
* Biological cultivation

It transforms a microscopic organism into something monitored, logged, controlled.

A synthetic biosphere.

---

## ARCHIVAL MESSAGE

**ARI:**
This codex was reconstructed from fragmented records and system logs.
Some data may be incomplete.

Further iterations are expected.

**The system is functional.**
**Replication is not encouraged.**

---

## ACCESS POINT

Full video record available via external archive (Youtube).

---

**End of Codex Entry**
