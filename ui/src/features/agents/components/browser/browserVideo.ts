export interface VideoMessage {
  key: boolean
  data: Uint8Array
}

const FLAG_KEY = 1
const FRAME_MICROSECONDS = 33_333

/** Splits a binary live-view message: a flag byte (1 for a key frame), then one Annex-B access unit. */
export function parseVideoMessage(buffer: ArrayBuffer): VideoMessage | null {
  const bytes = new Uint8Array(buffer)
  if (bytes.length < 2) return null
  return {
    key: ((bytes[0] ?? 0) & FLAG_KEY) === FLAG_KEY,
    data: bytes.subarray(1),
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

  /** The server announces the codec before the first frame of an encode and whenever it changes. */
  setCodec(codec: string): void {
    if (this.disposed || codec === this.codec) return
    this.configure(codec)
  }

  push(buffer: ArrayBuffer): void {
    const message = parseVideoMessage(buffer)
    if (!message || this.disposed) return
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
