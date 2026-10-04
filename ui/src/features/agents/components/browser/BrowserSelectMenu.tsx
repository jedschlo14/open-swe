import { useEffect, useRef } from "react"

import { cn } from "@/lib/utils"

export interface SelectMenuOption {
  value: string
  label: string
  selected: boolean
  disabled: boolean
}

/** A page `<select>` the viewer opened, placed under it in page coordinates. */
export interface SelectMenuState {
  id: number
  x: number
  y: number
  width: number
  options: SelectMenuOption[]
}

/** Stands in for the native popup a page's `<select>` opens, which a replayed document can't show. */
export function BrowserSelectMenu(props: {
  menu: SelectMenuState
  onPick: (value: string) => void
  onClose: () => void
}) {
  const { menu, onPick, onClose } = props
  const listRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return
      event.preventDefault()
      onClose()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [onClose])

  useEffect(() => {
    listRef.current
      ?.querySelector("[aria-selected=true]")
      ?.scrollIntoView({ block: "nearest" })
  }, [])

  return (
    <>
      <button
        type="button"
        aria-label="Close the list"
        className="absolute inset-0 cursor-default"
        onPointerDown={onClose}
      />
      <div
        ref={listRef}
        role="listbox"
        className="absolute z-10 max-h-64 overflow-y-auto rounded-md border border-border bg-popover py-1 text-sm text-popover-foreground shadow-md"
        style={{
          left: menu.x,
          top: menu.y,
          minWidth: Math.max(menu.width, 120),
        }}
      >
        {menu.options.map((option, index) => (
          <button
            // The page's own options can repeat values, so the position keeps keys unique.
            key={index}
            type="button"
            role="option"
            aria-selected={option.selected}
            disabled={option.disabled}
            className={cn(
              "block w-full cursor-pointer px-3 py-1.5 text-left hover:bg-accent disabled:cursor-default disabled:opacity-50",
              option.selected && "bg-accent/60 font-medium"
            )}
            onClick={() => {
              onPick(option.value)
              onClose()
            }}
          >
            {option.label || "\u00a0"}
          </button>
        ))}
      </div>
    </>
  )
}
