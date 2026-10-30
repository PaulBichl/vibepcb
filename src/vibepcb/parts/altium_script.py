"""Altium DelphiScript for a part: builds the symbol and the footprint natively in Altium Designer.

The KiCad import route can't carry everything the standard asks for (a pad on Mechanical 15, pin colours, the
3D body as a body object on Mechanical 13), so this writes the same geometry as Altium objects:

    out/parts/<mpn>/<mpn>.pas
        CreateFootprint   run with a PcbLib open: pads, overlay, Mech 13 body + pin-1 circle + 3D body,
                          Mech 15 placement outline, cross, .Designator (TrueType Arial), PIN1 pad
        CreateSymbol      run with a SchLib open: body, function drawing, pins, designator, footprint link

Run it with File » Run Script (or from a script project), once per library. The geometry comes from the generated
KiCad files (one source for both routes); KiCad's y axis points down in footprints, Altium's up, so y is negated
there. The Altium API calls follow Altium's scripting reference (DelphiScript); the script can't be run here,
check it on one part in Altium first.
"""

from __future__ import annotations

import itertools
import math
from typing import TYPE_CHECKING

from vibepcb import sexpr
from vibepcb.parts import footprint as fp_mod
from vibepcb.parts import spec as spec_mod

if TYPE_CHECKING:
    from vibepcb.parts.build import Built
    from vibepcb.sexpr import Node

# KiCad pin type -> Altium TPinElectrical
ELECTRICAL = {
    "input": "eElectricInput",
    "output": "eElectricOutput",
    "bidirectional": "eElectricIO",
    "tri_state": "eElectricHiZ",
    "passive": "eElectricPassive",
    "power_in": "eElectricPower",
    "power_out": "eElectricPower",
    "open_collector": "eElectricOpenCollector",
    "open_emitter": "eElectricOpenEmitter",
    "no_connect": "eElectricPassive",
    "unspecified": "eElectricPassive",
}
# KiCad pin angle (direction from the connection end towards the body) -> Altium orientation (from the body out)
ORIENTATION = {0: "eRotate180", 90: "eRotate270", 180: "eRotate0", 270: "eRotate90"}
STEP = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}  # KiCad symbol y points up, like Altium's

HELPERS = """
Function Mech(N : Integer) : TLayer;
Begin
  Result := LayerUtils.MechanicalLayer(N);
End;

Procedure AddPrim(Fp : IPCB_LibComponent; Prim : IPCB_Primitive);
Begin
  Fp.AddPCBObject(Prim);
  PCBServer.SendMessageToRobots(Fp.I_ObjectAddress, c_Broadcast, PCBM_BoardRegisteration, Prim.I_ObjectAddress);
End;

Procedure AddPad(Fp : IPCB_LibComponent; Name : String; X, Y, SX, SY, Hole, Rot : Double; Shape : TShape;
                 CornerPct : Integer; Layer : TLayer);
Var
  Pad : IPCB_Pad;
Begin
  Pad := PCBServer.PCBObjectFactory(ePadObject, eNoDimension, eCreate_Default);
  Pad.Mode := ePadMode_Simple;
  Pad.Name := Name;
  Pad.X := MMsToCoord(X);
  Pad.Y := MMsToCoord(Y);
  Pad.TopXSize := MMsToCoord(SX);
  Pad.TopYSize := MMsToCoord(SY);
  Pad.TopShape := Shape;
  Pad.Rotation := Rot;
  Pad.HoleSize := MMsToCoord(Hole);
  Pad.Layer := Layer;
  If Shape = eRoundedRectangular Then Pad.SetState_StackCRPctOnLayer(eTopLayer, CornerPct);
  Pad.SolderMaskExpansionMode := eMaskExpansionMode_Manual;   { §2.2: mask = pad }
  Pad.SolderMaskExpansion := 0;
  Pad.PasteMaskExpansionMode := eMaskExpansionMode_Manual;    { §2.2: paste = pad }
  Pad.PasteMaskExpansion := 0;
  AddPrim(Fp, Pad);
End;

Procedure AddTrack(Fp : IPCB_LibComponent; X1, Y1, X2, Y2, W : Double; Layer : TLayer);
Var
  T : IPCB_Track;
Begin
  T := PCBServer.PCBObjectFactory(eTrackObject, eNoDimension, eCreate_Default);
  T.X1 := MMsToCoord(X1); T.Y1 := MMsToCoord(Y1);
  T.X2 := MMsToCoord(X2); T.Y2 := MMsToCoord(Y2);
  T.Width := MMsToCoord(W);
  T.Layer := Layer;
  AddPrim(Fp, T);
End;

Procedure AddArc(Fp : IPCB_LibComponent; X, Y, R, W : Double; Layer : TLayer);
Var
  A : IPCB_Arc;
Begin
  A := PCBServer.PCBObjectFactory(eArcObject, eNoDimension, eCreate_Default);
  A.XCenter := MMsToCoord(X); A.YCenter := MMsToCoord(Y);
  A.Radius := MMsToCoord(R);
  A.LineWidth := MMsToCoord(W);
  A.StartAngle := 0; A.EndAngle := 360;
  A.Layer := Layer;
  AddPrim(Fp, A);
End;

Procedure AddText(Fp : IPCB_LibComponent; S : String; X, Y, H, Rot : Double; Layer : TLayer);
Var
  T : IPCB_Text;
Begin
  T := PCBServer.PCBObjectFactory(eTextObject, eNoDimension, eCreate_Default);
  T.Text := S;
  T.Layer := Layer;
  T.TextKind := eText_TrueTypeFont;                            { §2.1: TrueType }
  T.FontName := 'Arial';
  T.Size := MMsToCoord(H);
  T.Rotation := Rot;
  T.XLocation := MMsToCoord(X);
  T.YLocation := MMsToCoord(Y);
  AddPrim(Fp, T);
End;

Procedure AddBody(Fp : IPCB_LibComponent; StepFile : String; Layer : TLayer);
Var
  Body  : IPCB_ComponentBody;
  Model : IPCB_Model;
Begin
  If Not FileExists(StepFile) Then
  Begin
    ShowMessage('3D model not found: ' + StepFile + ' (no 3D body added)');
    Exit;
  End;
  Body := PCBServer.PCBObjectFactory(eComponentBodyObject, eNoDimension, eCreate_Default);
  Model := Body.ModelFactory_FromFilename(StepFile, False);
  Body.Model := Model;
  Body.Layer := Layer;
  AddPrim(Fp, Body);
End;

Function SchFont(Size : Integer) : Integer;
Begin
  Result := SchServer.FontManager.GetFontID(Size, 0, False, False, False, False, 'Arial');
End;

Procedure SchAdd(Comp : ISch_Component; Obj : ISch_GraphicalObject);
Begin
  Obj.OwnerPartId := Comp.CurrentPartID;
  Obj.OwnerPartDisplayMode := Comp.DisplayMode;
  Comp.AddSchObject(Obj);
  SchServer.RobotManager.SendMessage(Comp.I_ObjectAddress, c_BroadCast, SCHM_PrimitiveRegistration, Obj.I_ObjectAddress);
End;

Procedure SchRect(Comp : ISch_Component; X0, Y0, X1, Y1 : Double; Color : TColor);
Var
  R : ISch_Rectangle;
Begin
  R := SchServer.SchObjectFactory(eRectangle, eCreate_Default);
  R.Location := Point(MMsToCoord(X0), MMsToCoord(Y0));
  R.Corner := Point(MMsToCoord(X1), MMsToCoord(Y1));
  R.LineWidth := eMedium;                                     { §1.4 border medium }
  R.Color := Color;                                           { §1.4 border color }
  R.IsSolid := False;                                         { §1.4 draw solid no }
  SchAdd(Comp, R);
End;

Procedure SchLine(Comp : ISch_Component; X0, Y0, X1, Y1 : Double; Color : TColor);
Var
  L : ISch_Line;
Begin
  L := SchServer.SchObjectFactory(eLine, eCreate_Default);
  L.Location := Point(MMsToCoord(X0), MMsToCoord(Y0));
  L.Corner := Point(MMsToCoord(X1), MMsToCoord(Y1));
  L.LineWidth := eSmall;
  L.Color := Color;
  SchAdd(Comp, L);
End;

Procedure SchArc(Comp : ISch_Component; X, Y, R, A0, A1 : Double; Color : TColor);
Var
  A : ISch_Arc;
Begin
  A := SchServer.SchObjectFactory(eArc, eCreate_Default);
  A.Location := Point(MMsToCoord(X), MMsToCoord(Y));
  A.Radius := MMsToCoord(R);
  A.StartAngle := A0; A.EndAngle := A1;
  A.LineWidth := eSmall;
  A.Color := Color;
  SchAdd(Comp, A);
End;

Procedure SchPin(Comp : ISch_Component; Num, Name : String; X, Y, Len : Double; Orient : TRotationBy90;
                 Elec : TPinElectrical; ShowName : Boolean);
Var
  P : ISch_Pin;
Begin
  P := SchServer.SchObjectFactory(ePin, eCreate_Default);
  P.Location := Point(MMsToCoord(X), MMsToCoord(Y));          { the body end; the pin points away from it }
  P.Orientation := Orient;
  P.PinLength := MMsToCoord(Len);
  P.Designator := Num;
  P.Name := Name;
  P.Electrical := Elec;
  P.Color := 0;                                               { §1.4 pins black }
  P.ShowName := ShowName;
  SchAdd(Comp, P);
End;
"""


def _f(v: float) -> str:
    return f"{round(v, 4) + 0.0:g}"  # + 0.0: no "-0"


def _q(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _bgr(rgb: list[int]) -> str:
    r, g, b = rgb
    return str(b * 65536 + g * 256 + r)  # Altium TColor is BGR


def _num(node: list[Node], key: str, i: int = 1) -> float:
    child = sexpr.child(node, key)
    return float(str(child[i])) if child is not None else 0.0


def _layer(node: list[Node], mech13: str, mech15: str) -> str | None:
    layer = sexpr.child(node, "layer")
    name = str(layer[1]) if layer else ""
    return {"F.SilkS": "eTopOverlay", mech13: "Mech(13)", mech15: "Mech(15)"}.get(name)


def footprint_calls(built: Built) -> list[str]:
    """Altium calls for the footprint, y negated (KiCad footprints: y down)."""
    std = built.standard
    mech13, mech15 = std.text("layers", "mech13"), std.text("layers", "mech15")
    fp = built.footprint
    pin1_name = std.text("footprint", "pin1_text")
    mark, how = fp_mod.polarity_pad(built.spec, fp.pads)
    pin1 = mark if how == "pin1" and pin1_name else None
    calls: list[str] = []
    for node in fp.tree:
        if not isinstance(node, list):
            continue
        head = sexpr.head(node)
        layer = _layer(node, mech13, mech15)
        if head == "pad":
            at = sexpr.child(node, "at") or []
            size = sexpr.child(node, "size") or []
            x, y = float(str(at[1])), float(str(at[2]))
            rot = float(str(at[3])) if len(at) > 3 else 0.0
            sx, sy = float(str(size[1])), float(str(size[2]))
            hole = _num(node, "drill")
            shape = {
                "roundrect": "eRoundedRectangular",
                "rect": "eRectangular",
                "circle": "eRounded",
                "oval": "eRounded",
            }[str(node[3])]
            ratio = _num(node, "roundrect_rratio")
            pct = round(ratio * 200)  # KiCad: radius / smaller side; Altium: % of half the smaller side
            pad_layer = "eMultiLayer" if hole else "eTopLayer"
            calls.append(
                f"AddPad(Fp, {_q(str(node[1]))}, {_f(x)}, {_f(-y)}, {_f(sx)}, {_f(sy)}, {_f(hole)}, {_f(rot)},"
                f" {shape}, {pct}, {pad_layer});"
            )
        elif head == "fp_line" and layer:
            x0, y0 = _num(node, "start"), _num(node, "start", 2)
            x1, y1 = _num(node, "end"), _num(node, "end", 2)
            w = _num(sexpr.child(node, "stroke") or [], "width")
            calls.append(f"AddTrack(Fp, {_f(x0)}, {_f(-y0)}, {_f(x1)}, {_f(-y1)}, {_f(w)}, {layer});")
        elif head == "fp_rect" and layer:
            x0, y0 = _num(node, "start"), _num(node, "start", 2)
            x1, y1 = _num(node, "end"), _num(node, "end", 2)
            if pin1 is not None and layer == "Mech(15)" and _same_box((x0, y0, x1, y1), pin1.box):
                continue  # the KiCad stand-in for the PIN1 pad
            w = _num(sexpr.child(node, "stroke") or [], "width")
            for a, b, c, d in ((x0, y0, x1, y0), (x1, y0, x1, y1), (x1, y1, x0, y1), (x0, y1, x0, y0)):
                calls.append(f"AddTrack(Fp, {_f(a)}, {_f(-b)}, {_f(c)}, {_f(-d)}, {_f(w)}, {layer});")
        elif head == "fp_circle" and layer:
            cx, cy = _num(node, "center"), _num(node, "center", 2)
            r = math.hypot(_num(node, "end") - cx, _num(node, "end", 2) - cy)
            w = _num(sexpr.child(node, "stroke") or [], "width")
            filled = sexpr.child(node, "fill") == ["fill", "yes"]
            if filled:  # a dot: an arc of half the outer radius drawn as wide as that radius
                calls.append(f"AddArc(Fp, {_f(cx)}, {_f(-cy)}, {_f((r + w / 2) / 2)}, {_f(r + w / 2)}, {layer});")
            else:
                calls.append(f"AddArc(Fp, {_f(cx)}, {_f(-cy)}, {_f(r)}, {_f(w)}, {layer});")
        elif head == "fp_text" and layer:
            text = str(node[2])
            if pin1 is not None and text == pin1_name:
                continue
            at = sexpr.child(node, "at") or []
            size = _num(sexpr.child(sexpr.child(node, "effects") or [], "font") or [], "size")
            x, y = float(str(at[1])), float(str(at[2]))
            # Altium places text by its lower left corner: shift from KiCad's centre (Arial ~0.6 x height wide)
            width = 0.6 * size * len(text)
            calls.append(f"AddText(Fp, {_q(text)}, {_f(x - width / 2)}, {_f(-y - size / 2)}, {_f(size)}, 0, {layer});")
    if pin1 is not None:  # §2.1 note, Figure 11: pad 1 copied onto Mech 15, named PIN1 (no copper on Mech 15)
        calls.append(
            f"AddPad(Fp, {_q(pin1_name)}, {_f(pin1.x)}, {_f(-pin1.y)}, {_f(pin1.l)}, {_f(pin1.w)}, 0, 0,"
            f" eRectangular, 0, Mech(15));"
        )
    return calls


def _same_box(a: tuple[float, float, float, float], box: fp_mod.Box) -> bool:
    x0, y0, x1, y1 = a
    return all(abs(u - v) < 1e-3 for u, v in ((x0, box.x0), (y0, box.y0), (x1, box.x1), (y1, box.y1)))


def symbol_calls(built: Built) -> list[str]:
    """Altium calls for the symbol (KiCad symbols already have y up)."""
    std = built.standard
    sym = built.symbol
    color = _bgr(std.value("symbol", "body_color"))
    length = std.num("symbol", "pin_length")
    calls: list[str] = []
    for node in sexpr.walk(sym.tree):
        head = sexpr.head(node)
        if head == "rectangle":
            x0, y0 = _num(node, "start"), _num(node, "start", 2)
            x1, y1 = _num(node, "end"), _num(node, "end", 2)
            calls.append(f"SchRect(Comp, {_f(x0)}, {_f(y0)}, {_f(x1)}, {_f(y1)}, {color});")
        elif head == "polyline":
            pts = [(float(str(p[1])), float(str(p[2]))) for p in sexpr.children(sexpr.child(node, "pts") or [], "xy")]
            for (a, b), (c, d) in itertools.pairwise(pts):
                calls.append(f"SchLine(Comp, {_f(a)}, {_f(b)}, {_f(c)}, {_f(d)}, {color});")
        elif head == "arc":
            (sx, sy), (mx, my), (ex, ey) = ((_num(node, k), _num(node, k, 2)) for k in ("start", "mid", "end"))
            centre = _circle_centre((sx, sy), (mx, my), (ex, ey))
            if centre is None:
                continue
            cx, cy = centre
            r = math.hypot(sx - cx, sy - cy)
            a0 = math.degrees(math.atan2(sy - cy, sx - cx)) % 360
            a1 = math.degrees(math.atan2(ey - cy, ex - cx)) % 360
            am = math.degrees(math.atan2(my - cy, mx - cx)) % 360
            if not (a0 <= am <= a1 or (a0 > a1 and (am >= a0 or am <= a1))):
                a0, a1 = a1, a0  # Altium arcs run counter-clockwise from start to end
            calls.append(f"SchArc(Comp, {_f(cx)}, {_f(cy)}, {_f(r)}, {_f(a0)}, {_f(a1)}, {color});")
    by_number = {p.number: p for p in sym.pins}
    names = sexpr.child(sym.tree, "pin_names")
    show = "False" if names is not None and sexpr.child(names, "hide") is not None else "True"
    for number, (x, y, angle) in sym.pin_at.items():
        dx, dy = STEP[angle]
        pin = by_number[number]
        calls.append(
            f"SchPin(Comp, {_q(number)}, {_q(pin.name)}, {_f(x + dx * length)}, {_f(y + dy * length)}, {_f(length)},"
            f" {ORIENTATION[angle]}, {ELECTRICAL[pin.type]}, {show});"
        )
    return calls


def _circle_centre(
    a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]
) -> tuple[float, float] | None:
    (ax, ay), (bx, by), (cx, cy) = a, b, c
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    ux = ((ax**2 + ay**2) * (by - cy) + (bx**2 + by**2) * (cy - ay) + (cx**2 + cy**2) * (ay - by)) / d
    uy = ((ax**2 + ay**2) * (cx - bx) + (bx**2 + by**2) * (ax - cx) + (cx**2 + cy**2) * (bx - ax)) / d
    return ux, uy


def script(built: Built, step_path: str = "") -> str:
    """The DelphiScript for one part."""
    s = built.spec
    std = built.standard
    sym = built.symbol
    fp = built.footprint
    ref = sexpr.property_node(sym.tree, "Reference")
    at = sexpr.child(ref, "at") if ref else None
    rx, ry = (float(str(at[1])), float(str(at[2]))) if at else (sym.box[0], sym.box[3] + 1)
    prefix = sexpr.property_value(sym.tree, "Reference") or "U"
    designator_pt = round(std.num("symbol", "designator_font_pt"))
    designator_color = _bgr(std.value("symbol", "text_color"))
    height = s.package.height
    step = step_path or (built.step_file.name if built.step_file else "")
    indent = "  "
    fp_body = "\n".join(indent + c for c in footprint_calls(built))
    sym_body = "\n".join(indent + c for c in symbol_calls(built))
    body_line = f"{indent}AddBody(Fp, ScriptDir + {_q(step)}, Mech(13));" if step else f"{indent}{{ no STEP model }}"
    return f"""{{ VibePCB part {s.mpn} ({s.manufacturer}), standard {std.name}.
  Generated by `pcb part build` from parts/{spec_mod.file_name(s.mpn)}.toml - do not edit; change the spec.
  CreateFootprint: run with the target PcbLib open.  CreateSymbol: run with the target SchLib open. }}

Const
  ScriptDir = '';  {{ folder of {step or "the STEP"} with a trailing backslash; '' = Altium's current folder }}
{HELPERS}
Procedure CreateFootprint;
Var
  Lib : IPCB_Library;
  Fp  : IPCB_LibComponent;
Begin
  Lib := PCBServer.GetCurrentPCBLibrary;
  If Lib = Nil Then
  Begin
    ShowMessage('Open (focus) a PcbLib first.');
    Exit;
  End;
  Fp := PCBServer.CreatePCBLibComp;
  Fp.Name := {_q(fp.name)};
  Fp.Description := {_q(s.description)};
  Fp.Height := MMsToCoord({_f(height)});
  Lib.RegisterComponent(Fp);
  PCBServer.PreProcess;
{fp_body}
{body_line}
  PCBServer.PostProcess;
  Lib.CurrentComponent := Fp;
  Lib.Board.ViewManager_FullUpdate;
End;

Procedure CreateSymbol;
Var
  Lib  : ISch_Lib;
  Comp : ISch_Component;
  Impl : ISch_Implementation;
Begin
  If SchServer = Nil Then Exit;
  Lib := SchServer.GetCurrentSchDocument;
  If (Lib = Nil) Or (Lib.ObjectID <> eSchLib) Then
  Begin
    ShowMessage('Open (focus) a SchLib first.');
    Exit;
  End;
  Comp := SchServer.SchObjectFactory(eSchComponent, eCreate_Default);
  Comp.CurrentPartID := 1;
  Comp.DisplayMode := 0;
  Comp.LibReference := {_q(s.mpn)};
  Comp.ComponentDescription := {_q(s.description)};
  Comp.Designator.Text := {_q(prefix + "?")};                 {{ §1.3 part dependent, visible, top left }}
  Comp.Designator.Location := Point(MMsToCoord({_f(rx)}), MMsToCoord({_f(ry)}));
  Comp.Designator.FontID := SchFont({designator_pt});         {{ §1.3 Arial {designator_pt} }}
  Comp.Designator.Color := {designator_color};
  Comp.Designator.IsHidden := False;
  Comp.Designator.Autoposition := False;                     {{ §1.3 auto position off }}
  Comp.Comment.Text := '';                                   {{ §1.3 comment empty and hidden }}
  Comp.Comment.IsHidden := True;
  Lib.AddSchComponent(Comp);
  SchServer.RobotManager.SendMessage(Nil, c_BroadCast, SCHM_PrimitiveRegistration, Comp.I_ObjectAddress);
{sym_body}
  Impl := Comp.AddSchImplementation;                         {{ footprint link, no parameters (§1.3) }}
  Impl.ModelName := {_q(fp.name)};
  Impl.ModelType := 'PCBLIB';
  Impl.IsCurrent := True;
  Lib.CurrentSchComponent := Comp;
  Lib.GraphicallyInvalidate;
End;
"""
