import { EventType } from "@rrweb/types"
import type { eventWithTime } from "@rrweb/types"

export const REPLAY_QUIET_MS = 600
export const REPLAY_MAX_HOLD_MS = 2_500

/**
 * Keeps a freshly loaded document off screen until it stops changing. The
 * agent's page streams in half built, so painting every step shows sections
 * appearing and vanishing; holding the new document behind the old one shows
 * only the settled result. The first document is never held, and a document
 * that never settles is shown after a fixed wait.
 */
export class ReplayGate {
  private ready: eventWithTime[] = []
  private held: eventWithTime[] = []
  private holdingSince: number | null = null
  private lastEventAt = 0
  private shown = false

  constructor(
    private readonly quietMs = REPLAY_QUIET_MS,
    private readonly maxHoldMs = REPLAY_MAX_HOLD_MS
  ) {}

  push(events: eventWithTime[], now: number): void {
    if (!events.length) return
    if (
      this.holdingSince === null &&
      this.shown &&
      events.some((event) => event.type === EventType.FullSnapshot)
    )
      this.holdingSince = now
    if (this.holdingSince === null) {
      this.ready.push(...events)
      return
    }
    this.held.push(...events)
    this.lastEventAt = now
  }

  /** What may be painted now; a held document is released once quiet or overdue. */
  take(now: number): eventWithTime[] {
    if (this.holdingSince !== null && this.settled(now, this.holdingSince)) {
      this.ready.push(...this.held)
      this.held = []
      this.holdingSince = null
    }
    const events = this.ready
    this.ready = []
    if (!this.shown && events.some((e) => e.type === EventType.FullSnapshot))
      this.shown = true
    return events
  }

  /** Milliseconds until `take` could release something, or null when nothing waits. */
  wait(now: number): number | null {
    if (this.ready.length) return 0
    if (this.holdingSince === null) return null
    return Math.max(
      0,
      Math.min(
        this.holdingSince + this.maxHoldMs - now,
        this.lastEventAt + this.quietMs - now
      )
    )
  }

  reset(): void {
    this.ready = []
    this.held = []
    this.holdingSince = null
    this.shown = false
  }

  private settled(now: number, since: number): boolean {
    return (
      now - since >= this.maxHoldMs || now - this.lastEventAt >= this.quietMs
    )
  }
}
