import { describe, expect, it } from "vitest"

import { viewportFor } from "@/features/agents/components/browser/browserViewport"

describe("viewportFor", () => {
  it("asks for the panel's own size so the page is not scaled", () => {
    expect(viewportFor({ width: 733, height: 601 })).toEqual({
      width: 732,
      height: 600,
    })
  })

  it("keeps the page within what the browser supports", () => {
    expect(viewportFor({ width: 5000, height: 4000 })).toEqual({
      width: 1920,
      height: 1200,
    })
    expect(viewportFor({ width: 200, height: 100 })).toEqual({
      width: 320,
      height: 240,
    })
  })

  it("leaves the page alone while the panel is hidden", () => {
    expect(viewportFor({ width: 0, height: 0 })).toBeNull()
    expect(viewportFor({ width: Number.NaN, height: 500 })).toBeNull()
  })
})
