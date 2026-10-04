import { describe, expect, it } from "vitest"

import { parseScreen } from "@/features/agents/components/browser/browserMessages"

describe("the display size message", () => {
  it("gives the page's CSS size from the display's pixels", () => {
    expect(
      parseScreen({ type: "screen", width: 1440, height: 900, scale: 1.5 })
    ).toEqual({
      width: 1440,
      height: 900,
      scale: 1.5,
      cssWidth: 960,
      cssHeight: 600,
    })
    expect(parseScreen({ width: 1440, height: 900, scale: 0 })).toBeNull()
  })
})
