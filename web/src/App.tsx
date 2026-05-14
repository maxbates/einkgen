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
              onClick={() => setActive(t.id)}
            >
              {t.label}
            </button>
          ))}
        </nav>
      </header>
      <main className="app-main">
        {active === "queue" && <Queue />}
        {active === "history" && <History />}
        {active === "device" && <Device />}
      </main>
    </div>
  );
}
