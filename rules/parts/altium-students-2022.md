# Symbol and Footprint Generation Standards (Rev 2022, for students)

Source: `Symbol_and_Footprint_Generation_Standards_Rev2022_for_Students.pdf` (8 pages, written for Altium).
Text kept verbatim; figures are referenced by number only (see the PDF). Machine rules:
`altium-students-2022.toml`, each rule pointing back to a section here.

## 1. Symbol creation

After clicking on "Add symbol" on the corresponding folder, the schematic editor is opened. There are two options
when creating a new symbol, either using the wizard (Tools > Symbol Wizard) or drawing it manually.

### 1.1 Symbol Wizard

The wizard is useful when creating symbols with many pins, since Altium is automatically arranging them. However,
a mil grid will be taken.

*Figure 1.- Symbol wizard for a 80-pin microcontroller*

### 1.2 Drawing a symbol

Our requirements for symbols:

- Distance between pins should be at least 2mm for ICs.
- The signal flow in the component should go from left to right, that means input pins on the left side and output
  pins on the right side.
- Supply pins are placed on top and ground pins on bottom. Repeated pins should be named with consecutive numbers
  (e.g., GND1, GND2, etc.)
- Exposed Pads must be included as an additional pin.
- Any additional drawing or indication of the functionality of the component (e.g., drawing Operational Amplifiers
  as a triangle) might be helpful.

As an example:

*Figure 2.- Always reflect the function of the component. Exposed pad (EP) as an additional pin.*

### 1.3 Designator and parameters

The option "Show Comment/Designator" under "Tools > Document Options" should be checked see them on the schematic
editor. When this is done, the next steps are:

- The designator value is part dependent (e.g. D?, R?, U?..) and should be visible on the top-left part of the
  symbol.
- The comment value should be left empty and hidden.
- Also, no parameters are added (in Nexus parameters are defined in the component-level and not for the symbol like
  in Altium 17)

| Designator | |
|---|---|
| Default Comment | U?, R?, C? .. |
| Font | Arial 8 |
| Color | Black |
| Auto position | Off |

*Figure 3.- Adapting symbol appearance*

### 1.4 Schematic Default Primitives

These primitives are already set by default in the preferences:

| Ports | | Pins | | Sheet entries | |
|---|---|---|---|---|---|
| Height | 2mm | Length | 2mm | Font | Arial 6 |
| Font | Arial 6 | Electrical type | Passive | Fill color | 219 |
| Fill color | 219 | Name font | Arial 6 | Text color | 221 |
| Text color | 221 | Designator pos. | 0.5mm | Border color | 221 |
| Alignment | Right | Designator font | Arial 6 | | |
| Border color | 221 | Color | Black | | |
| Border width | smallest | | | | |

| Net Labels | | Rectangle/Polygon | | Parameters | |
|---|---|---|---|---|---|
| Font | Arial 6 | Border width | normal | Font | Arial 6 |
| Text color | 221 | Draw solid | No | Justification | Bottom Left |
| | | Transparent | No | Auto position | on |
| | | Fill Color | No | | |
| | | Border Color | 229 | | |
| | | Border | Medium | | |

*Figure 4.- Setting schematic defaults in the preferences*

## 2. Footprints/Geometries

After clicking on "Add Footprint" on the corresponding folder, the PCB editor is opened.

Non-standard footprints such as through-hole or connectors might be imported or drawn manually.

In general, footprints will be drawn using the "IPC Compliant Wizard" taking datasheet-drawings as a reference.
Whenever footprint dimensions are not given, let Altium use calculated values.

*Figure 5.- Footprints dimensions*

Medium Density is the preferred board density level for all footprints. In any case, the reference of the
component will be located in the middle-point of the Pads.

*Figure 6.- Component reference*

For automated inspection it is important that at least 0.2-0.3mm of copper remains exposed after the termination of
the component. Footprints created with Medium Density in Altium fulfill this condition, however this might need to
be checked for old/self-made footprints.

*Figure 7.- There shall be enough exposed copper for meniscus formation*

If you define the pads as "Rounded rectangle" – later the polygon will be adjusted with round corners.

*Figure 8.- define pad as "rounded rectangle"*
*Figure 9.- Repour polygon with rounded pads*
*Figure 10.- Repour polygon with rectangle pads*

### 2.1 Mechanical Layer Definition

| Layer | Name | Description |
|---|---|---|
| Mechanical 13 | Component Info Top | 3D Bodies and component outline (component body without pins) |
| Mechanical 15 | Assembly Info Top | Placement outline, center, designator, polarity, pin 1 position |

Mechanical 13 (Component Information)

- Includes the outline (ideally defined by the 3D-Model) of the component.

Mechanical 15 (Assembly Information)

- Includes the placement outline (0.1mm grid) of the component taking into account the maximal tolerances of
  package and Pads (0.1-0.3mm distance)
- Includes the reference of the component indicated with a small cross in its center.
- Includes the special string ".Designator" with "True type" Font. Without using ' before and after the special
  string
- Includes the polarity of the component:
  - Pin 1 for ICs.
  - Pin 1 and/or mounting polarity for Connectors
  - Cathode marking for diodes.

*Figure 11.- Overview of footprint layers*

Note: Change designator name from "1" to "PIN1" in order to avoid a Warning from Altium because of duplicated pin
designators.

*Figure 12.- Example of assembly information contained on Mech. 15*

### 2.2 Solder Mask and Paste Mask

Solder Mask extension: 0mm. Solder Mask should have the same size as the pad. This is because the PCB manufacturers
will expand the Solder Mask on their own.

Paste Mask extension: 0mm. Paste Mask should have the same size as the pad. This is because the PCB assembly
companies will make their own paste stencils.

*Figure 13.- Defining paste and solder mask expansion*

### 2.3 Silkscreen

Eurocircuits guidelines are taken as a reference. Clearance from pads to silkscreen should be at least 0.2mm
(otherwise those bits will be removed).

*Figure 14.- Eurocircuits recommendations for silkscreen*

Includes the polarity of the component:

- Pin 1 for ICs.
- Pin 1 and/or mounting polarity for Connectors
- Cathode marking for diodes.
- Positive terminal for polar capacitors.

At connectors, silkscreen should not be placed in front of pins, since it might cause changes in impedance at
impedance matched lines.
