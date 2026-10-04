import { describe, expect, it } from "vitest"

import {
  looksLikeLoginWall,
  signInTarget,
} from "@/features/agents/components/browser/browserSignIn"

const approved = ["github.com:443", "staging.example.com:8443"]

describe("signInTarget", () => {
  it("names the origin of an approved external page", () => {
    expect(signInTarget("https://github.com/login?x=1", approved)).toEqual({
      origin: "https://github.com",
      host: "github.com",
    })
    expect(
      signInTarget("https://staging.example.com:8443/a", approved)
    ).toEqual({
      origin: "https://staging.example.com:8443",
      host: "staging.example.com:8443",
    })
  })

  it("offers nothing before a page is open or off the approved endpoints", () => {
    expect(signInTarget(undefined, approved)).toBeNull()
    expect(signInTarget("about:blank", approved)).toBeNull()
    expect(signInTarget("http://localhost:3000", approved)).toBeNull()
    expect(signInTarget("https://other.com", approved)).toBeNull()
    expect(signInTarget("http://github.com", approved)).toBeNull()
  })
})

describe("looksLikeLoginWall", () => {
  it("recognizes sign-in pages by path", () => {
    expect(looksLikeLoginWall("https://github.com/login?return_to=%2F")).toBe(
      true
    )
    expect(looksLikeLoginWall("https://app.example.com/users/sign_in")).toBe(
      true
    )
    expect(looksLikeLoginWall("https://example.com/dashboard")).toBe(false)
    expect(looksLikeLoginWall("about:blank")).toBe(false)
    expect(looksLikeLoginWall(undefined)).toBe(false)
  })
})
