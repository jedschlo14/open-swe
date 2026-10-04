import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { SettingsRow, SettingsSection } from "@/components/AppShell"
import { Button } from "@/components/ui/button"
import {
  agentsApi,
  SAVED_SIGN_INS_QUERY_KEY,
  type SavedBrowserSignIn,
} from "@/features/agents/lib/api"
import { optimisticUpdate } from "@/lib/optimistic"

function describe(signIn: SavedBrowserSignIn): string {
  const day = (value: string) => new Date(value).toLocaleDateString()
  const state =
    signIn.status === "expired"
      ? `Expired ${day(signIn.expiresAt)}. Sign in again and save it to renew.`
      : `Expires ${day(signIn.expiresAt)}.`
  const used = signIn.lastUsedAt
    ? ` Last used ${day(signIn.lastUsedAt)}.`
    : " Not used yet."
  return `Saved ${day(signIn.createdAt)}. ${state}${used}`
}

/** The signed-in sessions the person saved from thread browsers, with revoke. */
export function SavedSignInsSection() {
  const queryClient = useQueryClient()
  const signIns = useQuery({
    queryKey: SAVED_SIGN_INS_QUERY_KEY,
    queryFn: agentsApi.getSavedBrowserSignIns,
  })

  const revoke = useMutation({
    meta: { errorTitle: "Couldn't revoke the saved sign-in" },
    mutationFn: (signInId: string) =>
      agentsApi.deleteSavedBrowserSignIn(signInId),
    onMutate: async (signInId) => ({
      undo: await optimisticUpdate<Array<SavedBrowserSignIn>>(
        queryClient,
        SAVED_SIGN_INS_QUERY_KEY,
        (current) => current.filter((item) => item.signInId !== signInId)
      ),
    }),
    onError: (_error, _signInId, context) => context?.undo(),
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: SAVED_SIGN_INS_QUERY_KEY }),
  })

  const items = signIns.data ?? []
  return (
    <SettingsSection
      title="Saved browser sign-ins"
      description="Sessions you saved from a thread's browser. Agents in your private threads can reuse them to sign in to the same site without asking you. Each lasts 30 days and covers one site. Revoking deletes our copy; it does not sign you out of the site."
    >
      {items.length === 0 ? (
        <div className="px-4 py-3.5 text-xs text-muted-foreground">
          {signIns.isPending
            ? "Loading…"
            : "None saved. Take control of a thread's browser, sign in, then tick Remember my sign-in when you hand control back."}
        </div>
      ) : (
        items.map((signIn) => (
          <SettingsRow
            key={signIn.signInId}
            label={signIn.origin}
            description={describe(signIn)}
            control={
              <Button
                size="sm"
                variant="outline"
                className="cursor-pointer"
                disabled={
                  revoke.isPending && revoke.variables === signIn.signInId
                }
                onClick={() => revoke.mutate(signIn.signInId)}
              >
                Revoke
              </Button>
            }
          />
        ))
      )}
    </SettingsSection>
  )
}
