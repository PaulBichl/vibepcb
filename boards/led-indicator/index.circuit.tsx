// Smoke-test board: 5 V header -> current-limiting resistor -> LED, GND poured on both layers.
// Every rail and GND gets a test point with a silkscreen label (`check` warns about a rail without one).
// Build with: hatch run pcb all led-indicator
export default () => (
  <board width="26mm" height="18mm">
    {/* ===== supply ===== */}
    <pinheader name="J1" pinCount={2} pitch="2.54mm" pinLabels={["VCC", "GND"]} showSilkscreenPinLabels
      pcbX={-9} pcbY={0} />

    {/* ===== indicator ===== */}
    <resistor name="R1" resistance="330" footprint="0603" pcbX={0} pcbY={2.5} />
    <led name="D1" color="red" footprint="0603" supplierPartNumbers={{ jlcpcb: ["C2286"] }} pcbX={0} pcbY={-2.5} />
    <silkscreentext text="PWR" pcbX={4} pcbY={-2.5} fontSize="0.8mm" />

    {/* ===== test points (one per rail; add one for every key debug signal) ===== */}
    <testpoint name="TP1" footprintVariant="pad" padShape="circle" padDiameter="1mm" pcbX={9} pcbY={6} />
    <silkscreentext text="VCC" pcbX={9} pcbY={4.2} fontSize="0.8mm" />
    <testpoint name="TP2" footprintVariant="pad" padShape="circle" padDiameter="1mm" pcbX={9} pcbY={-6} />
    <silkscreentext text="GND" pcbX={9} pcbY={-4.2} fontSize="0.8mm" />

    <silkscreentext text="led-indicator rev B" pcbX={0} pcbY={7.6} fontSize="0.9mm" />

    <trace name="J1_VCC" from=".J1 > .pin1" to="net.VCC" />
    <trace name="J1_GND" from=".J1 > .pin2" to="net.GND" />
    <trace name="R1_VCC" from="net.VCC" to=".R1 > .pin1" />
    <trace name="LED_A" from=".R1 > .pin2" to=".D1 > .anode" />
    <trace name="LED_K" from=".D1 > .cathode" to="net.GND" />
    <trace name="TP_VCC" from=".TP1 > .pin1" to="net.VCC" />
    <trace name="TP_GND" from=".TP2 > .pin1" to="net.GND" />
    <copperpour connectsTo="net.GND" layer="top" clearance="0.2mm" />
    <copperpour connectsTo="net.GND" layer="bottom" clearance="0.2mm" />
  </board>
)
