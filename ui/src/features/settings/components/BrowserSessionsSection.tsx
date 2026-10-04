import { useEffect, useState } from "react"
import { SettingsSection } from "@/components/AppShell"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import {
  useScopedSettings,
  type SettingsScope,
} from "@/features/settings/lib/settingsScope"
import { TierRow } from "./WorkspaceSettingsSections"

const MIN_IDLE_MINUTES = 5
const MAX_IDLE_MINUTES = 480

function endpointLines(value: ReadonlyArray<string> | null | undefined) {
  return (value ?? []).join("\n")
}

/** Idle timeout and approved external endpoints for thread browser sessions. */
export function BrowserSessionsSection({ scope }: { scope: SettingsScope }) {
  const settings = useScopedSettings(scope)
  const savedMinutes = settings.data?.browser_idle_timeout_minutes ?? 60
  const savedEndpoints = endpointLines(
    settings.data?.browser_approved_dev_endpoints
  )
  const [minutesDraft, setMinutesDraft] = useState(String(savedMinutes))
  const [endpointsDraft, setEndpointsDraft] = useState(savedEndpoints)

  useEffect(() => {
    // oxlint-disable-next-line react/set-state-in-effect
    setMinutesDraft(String(savedMinutes))
  }, [savedMinutes])
  useEffect(() => {
    // oxlint-disable-next-line react/set-state-in-effect
    setEndpointsDraft(savedEndpoints)
  }, [savedEndpoints])

  const saveMinutes = () => {
    const minutes = Number(minutesDraft)
    if (
      !Number.isInteger(minutes) ||
      minutes < MIN_IDLE_MINUTES ||
      minutes > MAX_IDLE_MINUTES
    ) {
      setMinutesDraft(String(savedMinutes))
      return
    }
    if (minutes !== savedMinutes)
      settings.save({ browser_idle_timeout_minutes: minutes })
  }

  const saveEndpoints = () => {
    const endpoints = endpointsDraft
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
    if (endpoints.join("\n") !== savedEndpoints)
      settings.save({ browser_approved_dev_endpoints: endpoints })
  }

  return (
    <SettingsSection
      title="Browser sessions"
      description="Each cloud thread can run one headless browser for checking the app it builds. It reaches only the thread's own sandbox servers and the endpoints approved here; clicking a sensitive-looking element on an approved external endpoint still needs a person's confirmation."
    >
      <div className="divide-y divide-border">
        <TierRow
          settings={settings}
          fields={["browser_idle_timeout_minutes"]}
          label="Idle timeout (minutes)"
          description="A browser nobody has used for this long is stopped, with a warning 5 minutes before. Between 5 and 480."
          control={
            <Input
              aria-label="Browser idle timeout in minutes"
              className="w-20"
              type="number"
              min={MIN_IDLE_MINUTES}
              max={MAX_IDLE_MINUTES}
              value={minutesDraft}
              onChange={(event) => setMinutesDraft(event.target.value)}
              onBlur={saveMinutes}
              disabled={!settings.data}
            />
          }
        />
        <TierRow
          settings={settings}
          fields={["browser_approved_dev_endpoints"]}
          label="Approved dev endpoints"
          description="External origins thread browsers may open, one per line, such as https://staging.example.com. Saved when you leave the field."
          control={
            <Textarea
              aria-label="Approved browser dev endpoints"
              className="min-h-20 w-64 font-mono text-xs"
              value={endpointsDraft}
              onChange={(event) => setEndpointsDraft(event.target.value)}
              onBlur={saveEndpoints}
              placeholder="https://staging.example.com"
              disabled={!settings.data}
            />
          }
        />
      </div>
    </SettingsSection>
  )
}
