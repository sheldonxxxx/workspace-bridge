export function mergeNewest<T>(
  fresh: T[],
  existing: T[],
  key: (item: T) => string,
): T[] {
  const freshKeys = new Set(fresh.map(key));
  return [...fresh, ...existing.filter((item) => !freshKeys.has(key(item)))];
}

export function appendOlder<T>(
  existing: T[],
  older: T[],
  key: (item: T) => string,
): T[] {
  const knownKeys = new Set(existing.map(key));
  return [...existing, ...older.filter((item) => !knownKeys.has(key(item)))];
}

/** A disjoint newest page cannot connect to retained history without paging. */
export function newestPageHasGap<T>(
  fresh: T[],
  existing: T[],
  key: (item: T) => string,
  hasOlderPage: boolean,
): boolean {
  if (!hasOlderPage || !fresh.length || !existing.length) return false;
  const freshKeys = new Set(fresh.map(key));
  return !existing.some((item) => freshKeys.has(key(item)));
}
