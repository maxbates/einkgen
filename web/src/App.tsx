import { useState } from "react";
import { Queue } from "./tabs/Queue";
import { History } from "./tabs/History";
import { Device } from "./tabs/Device";

type Tab = "queue" | "history" | "device";

const TABS: { id: Tab; label: string }[] = [
  { id: "queue", label: "Queue" },
  { id: "history", label: "History" },
  { id: "device", label: "Device" },
];

export function App() {
  const [active, setActive] = useState<Tab>("queue");
  // Bumped on every Device-tab click so the Device component remounts and
  // its useEffect re-runs even when the user re-clicks the active tab.
  const [deviceNonce, setDeviceNonce] = useState(0);

  function onTabClick(id: Tab) {
    if (id === "device") setDeviceNonce((n) => n + 1);
    setActive(id);
  }

  return (
    <div className="app">
      <header className="app-header">
        <h1 className="app-title">einkgen</h1>
        <nav className="tabs" role="tablist">
          {TABS.map((t) => (
            <button
              key={t.id}
              role="tab"
              aria-selected={active === t.id}
              className={`tab ${active === t.id ? "tab-active" : ""}`}
              onClick={() => onTabClick(t.id)}
            >
              {t.label}
            </button>
          ))}
        </nav>
      </header>
      <main className="app-main">
        {active === "queue" && <Queue />}
        {active === "history" && <History />}
        {active === "device" && <Device key={deviceNonce} />}
      </main>
    </div>
  );
}
