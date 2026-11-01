// Draw the app icon, in the Microsoft 365 (Fluent) manner: on a light plate, a
// sheet of notes made of stacked violet bands, and in front of it an indigo tile
// with a white waveform, casting a soft shadow on the sheet.
//
// Run: swift assets/app_icon.swift out.png   (1024 x 1024 PNG)
import AppKit

let S: CGFloat = 1024
let space = CGColorSpace(name: CGColorSpace.sRGB)!

func color(_ hex: UInt32, _ alpha: CGFloat = 1) -> CGColor {
    CGColor(srgbRed: CGFloat(hex >> 16 & 0xff) / 255, green: CGFloat(hex >> 8 & 0xff) / 255,
            blue: CGFloat(hex & 0xff) / 255, alpha: alpha)
}

func gradient(_ stops: [(CGColor, CGFloat)]) -> CGGradient {
    CGGradient(colorsSpace: space, colors: stops.map { $0.0 } as CFArray,
               locations: stops.map { $0.1 })!
}

func rounded(_ rect: CGRect, _ radius: CGFloat) -> CGPath {
    CGPath(roundedRect: rect, cornerWidth: radius, cornerHeight: radius, transform: nil)
}

// A context with the origin at the top left, like the numbers below.
let ctx = CGContext(data: nil, width: Int(S), height: Int(S), bitsPerComponent: 8,
                    bytesPerRow: 0, space: space,
                    bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
ctx.translateBy(x: 0, y: S)
ctx.scaleBy(x: 1, y: -1)

func fill(_ path: CGPath, _ g: CGGradient, from a: CGPoint, to b: CGPoint) {
    ctx.saveGState()
    ctx.addPath(path)
    ctx.clip()
    ctx.drawLinearGradient(g, start: a, end: b, options: [.drawsBeforeStartLocation, .drawsAfterEndLocation])
    ctx.restoreGState()
}

// The plate: Apple's grid, an 824 px rounded square centred in 1024.
let plateRect = CGRect(x: 100, y: 100, width: 824, height: 824)
let plate = rounded(plateRect, 185)
ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: 10), blur: 24, color: color(0x0e1030, 0.25))
ctx.addPath(plate)
ctx.setFillColor(color(0xffffff))
ctx.fillPath()
ctx.restoreGState()
fill(plate, gradient([(color(0xffffff), 0), (color(0xeceaf6), 1)]),
     from: CGPoint(x: 0, y: 100), to: CGPoint(x: 0, y: 924))

// The sheet: four bands, light at the top, deeper toward the bottom.
let sheetRect = CGRect(x: 400, y: 220, width: 400, height: 584)
let sheet = rounded(sheetRect, 72)
let bands: [(UInt32, UInt32)] = [(0xd3ceff, 0xbdb6fb), (0xaaa1f8, 0x948af1),
                                 (0x8476ea, 0x6e60df), (0x5f50d6, 0x4b3dc4)]
let bandH = sheetRect.height / CGFloat(bands.count)
ctx.saveGState()
ctx.addPath(sheet)
ctx.clip()
for (i, (top, bottom)) in bands.enumerated() {
    let r = CGRect(x: sheetRect.minX, y: sheetRect.minY + CGFloat(i) * bandH,
                   width: sheetRect.width, height: bandH)
    ctx.saveGState()
    ctx.clip(to: r)
    ctx.drawLinearGradient(gradient([(color(top), 0), (color(bottom), 1)]),
                           start: CGPoint(x: r.minX, y: r.minY), end: CGPoint(x: r.maxX, y: r.maxY),
                           options: [])
    ctx.restoreGState()
}
ctx.restoreGState()

// The tile, its shadow falling on the sheet.
let tileRect = CGRect(x: 224, y: 332, width: 360, height: 360)
let tile = rounded(tileRect, 66)
ctx.saveGState()
ctx.addPath(sheet)
ctx.clip()
ctx.setShadow(offset: CGSize(width: 14, height: 18), blur: 46, color: color(0x0e1030, 0.45))
ctx.addPath(tile)
ctx.setFillColor(color(0x2a1f8f))
ctx.fillPath()
ctx.restoreGState()
fill(tile, gradient([(color(0x5546dc), 0), (color(0x3427a8), 0.6), (color(0x231a7e), 1)]),
     from: CGPoint(x: tileRect.minX, y: tileRect.minY), to: CGPoint(x: tileRect.maxX, y: tileRect.maxY))
// a faint light on the tile's upper half
fill(tile, gradient([(color(0xffffff, 0.14), 0), (color(0xffffff, 0), 0.55)]),
     from: CGPoint(x: 0, y: tileRect.minY), to: CGPoint(x: 0, y: tileRect.maxY))

// The waveform.
ctx.setLineCap(.round)
ctx.setLineWidth(38)
ctx.setStrokeColor(color(0xffffff))
for (dx, h) in [(-120.0, 92.0), (-60.0, 170.0), (0.0, 236.0), (60.0, 160.0), (120.0, 84.0)]
    as [(CGFloat, CGFloat)] {
    let x = tileRect.midX + dx, half = (h - 38) / 2
    ctx.move(to: CGPoint(x: x, y: tileRect.midY - half))
    ctx.addLine(to: CGPoint(x: x, y: tileRect.midY + half))
}
ctx.strokePath()

let url = URL(fileURLWithPath: CommandLine.arguments[1])
let dest = CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil)!
CGImageDestinationAddImage(dest, ctx.makeImage()!, nil)
CGImageDestinationFinalize(dest)
