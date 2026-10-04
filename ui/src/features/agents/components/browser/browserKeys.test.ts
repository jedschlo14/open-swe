import { describe, expect, it } from "vitest"

import {
  planKey,
  type KeyEventLike,
} from "@/features/agents/components/browser/browserKeys"

function key(overrides: Partial<KeyEventLike>): KeyEventLike {
  return {
    key: "a",
    code: "KeyA",
    metaKey: false,
    ctrlKey: false,
    shiftKey: false,
    altKey: false,
    ...overrides,
  }
}

describe("planKey", () => {
  it("leaves paste to the browser so the local clipboard is what lands", () => {
    expect(planKey(key({ key: "v", ctrlKey: true }), "down", false)).toEqual({
      kind: "paste",
    })
    expect(planKey(key({ key: "v", metaKey: true }), "down", true)).toEqual({
      kind: "paste",
    })
  })

  it("maps ⌘ to Ctrl on a Mac and taps keys pressed under it", () => {
    expect(
      planKey(
        key({ key: "Meta", code: "MetaLeft", metaKey: true }),
        "down",
        true
      )
    ).toMatchObject({ key: "Control", code: "ControlLeft", tap: false })
    expect(
      planKey(key({ key: "a", metaKey: true }), "down", true)
    ).toMatchObject({ key: "a", tap: true })
    expect(planKey(key({ key: "a", metaKey: true }), "up", true)).toEqual({
      kind: "ignore",
    })
  })

  it("asks for the selection before a copy or cut runs", () => {
    expect(
      planKey(key({ key: "c", ctrlKey: true }), "down", false)
    ).toMatchObject({ copy: true })
    expect(
      planKey(key({ key: "c", ctrlKey: true }), "up", false)
    ).toMatchObject({ copy: false })
  })
})
