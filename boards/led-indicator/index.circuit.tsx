// Minimal example: 5 V header -> current-limiting resistor -> LED.
// Build with: hatch run pcb all led-indicator
export default () => (
  <board width="20mm" height="15mm">
    <pinheader
      name="J1"
      pinCount={2}
      pitch="2.54mm"
      pinLabels={["VCC", "GND"]}
      pcbX={-6}
      pcbY={0}
      schX={-4}
      schY={0}
    />
    <resistor name="R1" resistance="330" footprint="0603" pcbX={1} pcbY={2} schX={0} schY={1} />
    <led
      name="D1"
      color="red"
      footprint="0603"
      supplierPartNumbers={{ jlcpcb: ["C2286"] }}
      pcbX={1}
      pcbY={-2}
      schX={2}
      schY={0}
      schRotation={-90}
    />

    <trace name="J1_VCC" from=".J1 > .pin1" to="net.VCC" />
    <trace name="J1_GND" from=".J1 > .pin2" to="net.GND" />
    <trace name="R1_VCC" from="net.VCC" to=".R1 > .pin1" />
    <trace name="LED_A" from=".R1 > .pin2" to=".D1 > .anode" />
    <trace name="LED_K" from=".D1 > .cathode" to="net.GND" />
  </board>
)
