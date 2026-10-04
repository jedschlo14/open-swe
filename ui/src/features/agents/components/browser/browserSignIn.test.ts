import { describe, expect, it } from "vitest"

import {
  looksLikeLogin,
  pageOrigin,
  signInCompleted,
} from "@/features/agents/components/browser/browserSignIn"

const page = (url: string, loading = false) => ({ url, title: "", loading })

describe("signInCompleted", () => {
  const login = "https://app.example.com/login"

  it("is false before anything is typed", () => {
    expect(signInCompleted(null, page("https://app.example.com/home"))).toBe(
      false
    )
  })

  it("is false while the typed-into page is still showing", () => {
    expect(signInCompleted(login, page(login))).toBe(false)
  })

  it("is false while the next page loads or when it is another login page", () => {
    expect(
      signInCompleted(login, page("https://app.example.com/home", true))
    ).toBe(false)
    expect(
      signInCompleted(login, page("https://app.example.com/login/verify"))
    ).toBe(false)
  })

  it("is true once a non-login page has loaded", () => {
    expect(signInCompleted(login, page("https://app.example.com/home"))).toBe(
      true
    )
  })

  it("is false on blank and loopback pages", () => {
    expect(signInCompleted(login, page("about:blank"))).toBe(false)
    expect(signInCompleted(login, page("http://localhost:3000/home"))).toBe(
      false
    )
  })
})

describe("looksLikeLogin", () => {
  it("matches login paths and hosts but not lookalike words", () => {
    expect(looksLikeLogin("https://a.com/users/sign_in")).toBe(true)
    expect(looksLikeLogin("https://sso.a.com/start")).toBe(true)
    expect(looksLikeLogin("https://a.com/authors")).toBe(false)
    expect(looksLikeLogin("https://a.com/dashboard")).toBe(false)
  })
})

describe("pageOrigin", () => {
  it("returns the origin of external http(s) pages only", () => {
    expect(pageOrigin("https://a.com/x?y=1")).toBe("https://a.com")
    expect(pageOrigin("about:blank")).toBeNull()
    expect(pageOrigin(undefined)).toBeNull()
  })
})
