export interface VideoMessage {
  key: boolean
  /** The `avc1` codec string, present on key frames. */
  codec: string | null
  data: Uint8Array
}

const FLAG_KEY = 1
const CODEC_BYTES = 3
const FRAME_MICROSECONDS = 33_333

function hex(byte: number | undefined): string {
  return (byte ?? 0).toString(16).padStart(2, "0")
}

/** Splits a binary live-view message: a flag byte, codec bytes on key frames, then Annex-B data. */
export function parseVideoMessage(buffer: ArrayBuffer): VideoMessage | null {
  const bytes = new Uint8Array(buffer)
  if (bytes.length < 2) return null
  const key = ((bytes[0] ?? 0) & FLAG_KEY) === FLAG_KEY
  if (!key) return { key, codec: null, data: bytes.subarray(1) }
  if (bytes.length < 1 + CODEC_BYTES + 1) return null
  return {
    key,
    codec: `avc1.${hex(bytes[1])}${hex(bytes[2])}${hex(bytes[3])}`,
    data: bytes.subarray(1 + CODEC_BYTES),
  }
}

export function canPlayLiveView(): boolean {
  return (
    typeof VideoDecoder !== "undefined" &&
    typeof EncodedVideoChunk !== "undefined"
  )
}

/**
 * Decodes the live view's H.264 stream onto a canvas. Decoded frames wait for
 * the next animation frame and only the newest is drawn, so a viewer whose
 * display lags shows the current page rather than a backlog.
 */
export class BrowserVideoPlayer {
  private decoder: VideoDecoder | null = null
  private codec: string | null = null
  private timestamp = 0
  private pending: VideoFrame | null = null
  private scheduled = 0
  private disposed = false

  constructor(
    private readonly getCanvas: () => HTMLCanvasElement | null,
    private readonly onFailure: (error: unknown) => void
  ) {}

  push(buffer: ArrayBuffer): void {
    const message = parseVideoMessage(buffer)
    if (!message || this.disposed) return
    if (message.key && message.codec !== this.codec)
      this.configure(message.codec)
    const decoder = this.decoder
    if (!decoder || decoder.state !== "configured") return
    this.timestamp += FRAME_MICROSECONDS
    try {
      decoder.decode(
        new EncodedVideoChunk({
          type: message.key ? "key" : "delta",
          timestamp: this.timestamp,
          data: message.data,
        })
      )
    } catch (error) {
      this.onFailure(error)
    }
  }

  dispose(): void {
    this.disposed = true
    this.closeDecoder()
    if (this.scheduled) cancelAnimationFrame(this.scheduled)
    this.pending?.close()
    this.pending = null
  }

  private configure(codec: string | null): void {
    this.closeDecoder()
    if (!codec) return
    this.codec = codec
    const decoder = new VideoDecoder({
      output: (frame) => this.show(frame),
      error: (error) => this.onFailure(error),
    })
    decoder.configure({ codec, optimizeForLatency: true })
    this.decoder = decoder
  }

  private closeDecoder(): void {
    if (this.decoder && this.decoder.state !== "closed") this.decoder.close()
    this.decoder = null
    this.codec = null
    this.timestamp = 0
  }

  private show(frame: VideoFrame): void {
    this.pending?.close()
    this.pending = frame
    if (this.scheduled) return
    this.scheduled = requestAnimationFrame(() => {
      this.scheduled = 0
      const next = this.pending
      this.pending = null
      if (!next) return
      const canvas = this.getCanvas()
      if (canvas) {
        if (canvas.width !== next.displayWidth) canvas.width = next.displayWidth
        if (canvas.height !== next.displayHeight)
          canvas.height = next.displayHeight
        canvas.getContext("2d")?.drawImage(next, 0, 0)
      }
      next.close()
    })
  }
}
