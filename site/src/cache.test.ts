import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearSnapshot, readSnapshot, saveSnapshot } from "./cache";

const day = { date: "2026-11-01", total: 1, services: {}, projects: {}, usageTypes: {} };
const config = { apiUrl: "https://api", authUrl: "https://auth", clientId: "c", region: "us-east-1" };

describe("snapshot cache", () => {
  beforeEach(() => {
    const store = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    });
  });
  afterEach(() => vi.unstubAllGlobals());

  it("round-trips and clears", () => {
    expect(readSnapshot()).toBeNull();
    saveSnapshot({ config, days: [day] });
    expect(readSnapshot()).toEqual({ config, days: [day] });
    clearSnapshot();
    expect(readSnapshot()).toBeNull();
  });

  it("ignores a corrupt or wrong-shaped value", () => {
    localStorage.setItem("costwatcher.snapshot", "{not json");
    expect(readSnapshot()).toBeNull();
    localStorage.setItem("costwatcher.snapshot", JSON.stringify({ days: "nope" }));
    expect(readSnapshot()).toBeNull();
  });

  it("drops a snapshot whose days have the wrong shape, or another version", () => {
    localStorage.setItem("costwatcher.snapshot", JSON.stringify({ version: 1, config, days: [{}] }));
    expect(readSnapshot()).toBeNull();
    expect(localStorage.getItem("costwatcher.snapshot")).toBeNull();
    localStorage.setItem("costwatcher.snapshot", JSON.stringify({ config, days: [day] }));
    expect(readSnapshot()).toBeNull();
  });

  it("survives storage that throws", () => {
    vi.stubGlobal("localStorage", {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
      removeItem: () => {
        throw new Error("blocked");
      },
    });
    expect(readSnapshot()).toBeNull();
    expect(() => saveSnapshot({ config, days: [] })).not.toThrow();
    expect(() => clearSnapshot()).not.toThrow();
  });
});
