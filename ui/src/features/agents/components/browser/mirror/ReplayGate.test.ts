import { EventType } from "@rrweb/types"
import type { eventWithTime } from "@rrweb/types"
import { describe, expect, it } from "vitest"

import { ReplayGate } from "@/features/agents/components/browser/mirror/ReplayGate"

const snapshot = { type: EventType.FullSnapshot } as eventWithTime
const mutation = { type: EventType.IncrementalSnapshot } as eventWithTime

describe("ReplayGate", () => {
  it("paints the first document at once", () => {
    const gate = new ReplayGate(100, 1000)
    gate.push([snapshot, mutation], 0)
    expect(gate.take(0)).toEqual([snapshot, mutation])
  })

  it("keeps a new document off screen until it stops changing", () => {
    const gate = new ReplayGate(100, 1000)
    gate.push([snapshot], 0)
    gate.take(0)
    gate.push([snapshot], 500)
    gate.push([mutation], 550)
    expect(gate.take(600)).toEqual([])
    expect(gate.wait(600)).toBe(50)
    expect(gate.take(650)).toEqual([snapshot, mutation])
    expect(gate.wait(650)).toBeNull()
  })

  it("shows a document that never settles after the maximum hold", () => {
    const gate = new ReplayGate(100, 1000)
    gate.push([snapshot], 0)
    gate.take(0)
    gate.push([snapshot], 100)
    for (let at = 150; at < 1100; at += 50) gate.push([mutation], at)
    expect(gate.take(1050)).toEqual([])
    expect(gate.take(1100).length).toBeGreaterThan(1)
  })
})
