// mn-recorder: records a call to two files, the Mac's own audio and the microphone.
//
//   mn-recorder <folder>
//
// System audio comes from a Core Audio process tap (macOS 14.2+), read through a
// private aggregate device; the microphone through AVAudioEngine. Each goes to its
// own file, <folder>/system.wav and <folder>/mic.wav,
// 16 kHz mono 16-bit: what Whisper reads, and small enough to keep. Python reads
// the files while they grow, to transcribe the call as it happens.
//
// It records until its stdin closes or it gets SIGINT / SIGTERM, then finishes
// both files. Everything it has to say goes to stdout, one JSON object a line:
//   {"event":"started","system":{"rate":48000,"channels":1},"mic":{...},"at":1730000000.0}
//   {"event":"level","system":0.12,"mic":0.03,"system_frames":16000,"mic_frames":16000}
//                                     about once a second; frames written since the last
//   {"event":"warning","what":"..."}  a stream has delivered nothing for a few seconds
//   {"event":"error","what":"...","status":-1}
//   {"event":"stopped","seconds":63.2}
//
// Built by build.sh into the app bundle, next to the app's own executable, so
// the permission prompts name Meeting Notes and not whatever started it.

import AVFoundation
import CoreAudio
import Foundation

setvbuf(stdout, nil, _IOLBF, 0)

func say(_ object: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: object),
       let line = String(data: data, encoding: .utf8) {
        print(line)
    }
}

func fail(_ what: String, _ status: OSStatus = -1) -> Never {
    say(["event": "error", "what": what, "status": Int(status)])
    exit(1)
}

/// Peak level of the last buffer, 0...1, for the "is anything coming in" meter.
/// What gets written: 16 kHz mono 16-bit.
let target = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1,
                           interleaved: true)!

/// Converts whatever a source delivers into `target` and appends it to a file.
/// One per stream; called from that stream's own thread only. Only the first
/// channel is kept: a microphone array hands over several, and mixing them all
/// is what turned one into silence.
final class Writer {
    private let converter: AVAudioConverter
    private var file: AVAudioFile?
    private let lock = NSLock()
    private var written: AVAudioFrameCount = 0

    init(from format: AVAudioFormat, to url: URL) {
        guard let converter = AVAudioConverter(from: format, to: target) else {
            fail("cannot convert \(format) for \(url.lastPathComponent)")
        }
        if format.channelCount > 1 { converter.channelMap = [0] }
        self.converter = converter
        do {
            file = try AVAudioFile(forWriting: url, settings: target.settings,
                                   commonFormat: .pcmFormatInt16, interleaved: true)
        } catch { fail("could not open \(url.lastPathComponent): \(error)") }
    }

    func write(_ buffer: AVAudioPCMBuffer) {
        let ratio = target.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 64
        guard let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return }
        var given = false
        var error: NSError?
        converter.convert(to: out, error: &error) { _, status in
            if given { status.pointee = .noDataNow; return nil }
            given = true
            status.pointee = .haveData
            return buffer
        }
        if error != nil || out.frameLength == 0 { return }
        lock.lock()
        try? file?.write(from: out)
        written += out.frameLength
        lock.unlock()
    }

    /// Frames written since the last time this was asked.
    func takeWritten() -> Int {
        lock.lock(); defer { lock.unlock() }
        let n = Int(written); written = 0; return n
    }

    func close() {
        lock.lock(); file = nil; lock.unlock()
    }
}

final class Meter {
    private let lock = NSLock()
    private var peak: Float = 0
    func feed(_ buffer: AVAudioPCMBuffer) {
        guard let channels = buffer.floatChannelData else { return }
        var top: Float = 0
        for c in 0..<Int(buffer.format.channelCount) {
            for i in 0..<Int(buffer.frameLength) { top = max(top, abs(channels[c][i])) }
        }
        lock.lock(); peak = max(peak, top); lock.unlock()
    }
    func take() -> Float {
        lock.lock(); defer { lock.unlock() }
        let value = peak; peak = 0; return value
    }
}

// ------------------------------------------------------------------ system audio

func property<T: BitwiseCopyable>(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector,
                 _ initial: T) throws -> T {
    var address = AudioObjectPropertyAddress(mSelector: selector,
                                             mScope: kAudioObjectPropertyScopeGlobal,
                                             mElement: kAudioObjectPropertyElementMain)
    var size = UInt32(MemoryLayout<T>.size)
    var value = initial
    let status = AudioObjectGetPropertyData(object, &address, 0, nil, &size, &value)
    if status != noErr { throw NSError(domain: "coreaudio", code: Int(status)) }
    return value
}

/// A device's UID. A CFString comes back retained, so it is not read through
/// `property`, which is only for plain values.
func deviceUID(_ device: AudioObjectID) throws -> String {
    var address = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyDeviceUID,
                                             mScope: kAudioObjectPropertyScopeGlobal,
                                             mElement: kAudioObjectPropertyElementMain)
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    var uid: Unmanaged<CFString>?
    let status = AudioObjectGetPropertyData(device, &address, 0, nil, &size, &uid)
    guard status == noErr, let uid else { throw NSError(domain: "coreaudio", code: Int(status)) }
    return uid.takeRetainedValue() as String
}

final class SystemRecorder {
    let meter = Meter()
    private var tap = AudioObjectID(kAudioObjectUnknown)
    private var device = AudioObjectID(kAudioObjectUnknown)
    private var proc: AudioDeviceIOProcID?
    private(set) var writer: Writer?
    private let queue = DispatchQueue(label: "system-audio")
    private(set) var format: AVAudioFormat!

    func start(to url: URL) {
        // Everything the Mac plays, mixed to mono, left playing as it was.
        let description = CATapDescription(monoGlobalTapButExcludeProcesses: [])
        description.uuid = UUID()
        description.isPrivate = true
        description.muteBehavior = .unmuted
        var status = AudioHardwareCreateProcessTap(description, &tap)
        if status != noErr { fail("could not create the system audio tap", status) }

        guard var asbd = try? property(tap, kAudioTapPropertyFormat,
                                       AudioStreamBasicDescription()),
              let format = AVAudioFormat(streamDescription: &asbd)
        else { fail("could not read the tap's format") }
        self.format = format

        // A tap is read through an aggregate device that contains it.
        let output: AudioObjectID
        let outputUID: String
        do {
            output = try property(AudioObjectID(kAudioObjectSystemObject),
                                  kAudioHardwarePropertyDefaultSystemOutputDevice,
                                  AudioObjectID(kAudioObjectUnknown))
            outputUID = try deviceUID(output)
        } catch { fail("no output device to tap") }

        let aggregate: [String: Any] = [
            kAudioAggregateDeviceNameKey: "Meeting Notes tap",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceMainSubDeviceKey: outputUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outputUID]],
            kAudioAggregateDeviceTapListKey: [[kAudioSubTapDriftCompensationKey: true,
                                               kAudioSubTapUIDKey: description.uuid.uuidString]],
        ]
        status = AudioHardwareCreateAggregateDevice(aggregate as CFDictionary, &device)
        if status != noErr { fail("could not create the aggregate device", status) }

        writer = Writer(from: format, to: url)

        status = AudioDeviceCreateIOProcIDWithBlock(&proc, device, queue) {
            [weak self] _, input, _, _, _ in
            guard let self, let writer = self.writer,
                  let buffer = AVAudioPCMBuffer(pcmFormat: self.format,
                                                bufferListNoCopy: input, deallocator: nil)
            else { return }
            self.meter.feed(buffer)
            writer.write(buffer)
        }
        if status != noErr { fail("could not attach to the aggregate device", status) }
        status = AudioDeviceStart(device, proc)
        if status != noErr { fail("could not start system audio", status) }
    }

    func stop() {
        if let proc {
            AudioDeviceStop(device, proc)
            AudioDeviceDestroyIOProcID(device, proc)
        }
        queue.sync { writer?.close() }      // closes the file after the last write
        if device != kAudioObjectUnknown { AudioHardwareDestroyAggregateDevice(device) }
        if tap != kAudioObjectUnknown { AudioHardwareDestroyProcessTap(tap) }
    }
}

// ------------------------------------------------------------------ microphone

final class MicRecorder {
    let meter = Meter()
    private let engine = AVAudioEngine()
    private(set) var writer: Writer?
    private(set) var format: AVAudioFormat!

    // Apple's voice processing (echo cancellation) is deliberately not used: on
    // macOS it reconfigures the audio devices, which starved the system tap and
    // turned the microphone into a 9-channel stream of silence. Echo is handled
    // on the text instead, against the clean system stream.
    func start(to url: URL) {
        let input = engine.inputNode
        format = input.outputFormat(forBus: 0)
        if format.sampleRate == 0 { fail("no microphone") }
        writer = Writer(from: format, to: url)
        // Deprecated in macOS 27, still the documented way on 14–26, which is
        // what the app supports.
        input.installTap(onBus: 0, bufferSize: 4096, format: format) { [weak self] buffer, _ in
            guard let self else { return }
            self.meter.feed(buffer)
            self.writer?.write(buffer)
        }
        do { try engine.start() } catch { fail("could not start the microphone: \(error)") }
    }

    func stop() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        writer?.close()
    }
}

// ------------------------------------------------------------------ run

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write("usage: mn-recorder <folder>\n".data(using: .utf8)!)
    exit(2)
}
let folder = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)

// The microphone asks for permission once; the answer is remembered.
let asked = DispatchSemaphore(value: 0)
var micAllowed = false
AVCaptureDevice.requestAccess(for: .audio) { granted in micAllowed = granted; asked.signal() }
asked.wait()
if !micAllowed { fail("microphone permission was not given") }

let system = SystemRecorder()
let mic = MicRecorder()
system.start(to: folder.appendingPathComponent("system.wav"))
mic.start(to: folder.appendingPathComponent("mic.wav"))
let began = Date()

func describe(_ format: AVAudioFormat) -> [String: Any] {
    ["rate": format.sampleRate, "channels": Int(format.channelCount)]
}
say(["event": "started", "system": describe(system.format), "mic": describe(mic.format),
     "at": began.timeIntervalSince1970])

var finished = false
func finish() {
    if finished { return }
    finished = true
    system.stop()
    mic.stop()
    say(["event": "stopped", "seconds": Date().timeIntervalSince(began)])
    exit(0)
}

signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)
var sources: [DispatchSourceSignal] = []
for sig in [SIGINT, SIGTERM] {
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    source.setEventHandler { finish() }
    source.resume()
    sources.append(source)
}

// stdin closing means whoever started this has gone, or wants it to stop.
DispatchQueue.global().async {
    while let _ = readLine() {}
    DispatchQueue.main.async { finish() }
}

let ticker = DispatchSource.makeTimerSource(queue: .main)
ticker.schedule(deadline: .now() + 1, repeating: 1)
// A stream that writes nothing for a few seconds is broken, not quiet: the tap
// delivers zeros when nothing plays and a microphone always hears something.
var idle = ["system": 0, "mic": 0]
ticker.setEventHandler {
    let frames = ["system": system.writer?.takeWritten() ?? 0, "mic": mic.writer?.takeWritten() ?? 0]
    say(["event": "level", "system": system.meter.take(), "mic": mic.meter.take(),
         "system_frames": frames["system"]!, "mic_frames": frames["mic"]!])
    for (name, n) in frames {
        idle[name] = n == 0 ? idle[name]! + 1 : 0
        if idle[name] == 3 { say(["event": "warning", "what": "\(name == "mic" ? "the microphone" : "the system audio") has stopped delivering sound"]) }
    }
}
ticker.resume()

dispatchMain()
