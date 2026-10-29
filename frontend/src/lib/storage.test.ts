import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); vi.resetModules(); });

describe("user-scoped browser memory", () => {
  it("keeps different demo identities in separate caches", async () => {
    const data = new Map<string, string>();
    vi.stubGlobal("localStorage", { getItem: (key: string) => data.get(key) ?? null, setItem: (key: string, value: string) => data.set(key, value) });
    vi.stubEnv("VITE_DEMO_USER_ID", "alice");
    vi.resetModules();
    const alice = await import("./storage");
    alice.saveConversations([{ id: "private", title: "Alice", updatedAt: 0, messages: [] }]);
    vi.stubEnv("VITE_DEMO_USER_ID", "bob");
    vi.resetModules();
    const bob = await import("./storage");
    expect(bob.loadConversations()).toEqual([]);
    expect(alice.loadConversations()[0]?.title).toBe("Alice");
  });
});
