/*
 * Node.js 25+ defines an experimental `localStorage` global that stays
 * undefined unless --localstorage-file is given. Because the key already
 * exists on globalThis, Vitest's jsdom environment does not install the jsdom
 * implementation over it, and every store using localStorage fails. Provide a
 * minimal in-memory Storage so tests behave the same on every Node.js version.
 */
class MemoryStorage implements Storage {
  private map = new Map<string, string>()
  get length() {
    return this.map.size
  }
  clear() {
    this.map.clear()
  }
  getItem(key: string) {
    return this.map.has(key) ? (this.map.get(key) as string) : null
  }
  key(index: number) {
    return [...this.map.keys()][index] ?? null
  }
  removeItem(key: string) {
    this.map.delete(key)
  }
  setItem(key: string, value: string) {
    this.map.set(key, String(value))
  }
}

for (const name of ['localStorage', 'sessionStorage'] as const) {
  const current = (globalThis as Record<string, unknown>)[name] as Storage | undefined
  if (typeof current?.getItem !== 'function') {
    Object.defineProperty(globalThis, name, {
      value: new MemoryStorage(),
      configurable: true,
      writable: true
    })
  }
}
