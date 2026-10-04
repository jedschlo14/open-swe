import type { ImageChunk } from "@/features/agents/lib/types"

export interface ComposerDraft {
  value: string
  images: Array<ImageChunk>
}

const drafts = new Map<string, ComposerDraft>()

/** Unsent composer content held in memory so it survives switching threads, not a reload. */
export function readComposerDraft(
  key: string | undefined
): ComposerDraft | null {
  return key ? (drafts.get(key) ?? null) : null
}

export function writeComposerDraft(
  key: string | undefined,
  draft: ComposerDraft
) {
  if (!key) return
  if (draft.value.length === 0 && draft.images.length === 0) {
    drafts.delete(key)
    return
  }
  drafts.set(key, draft)
}
