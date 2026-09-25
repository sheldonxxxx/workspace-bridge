export declare const RELEASE_CONTRACT: number;
export declare const RELEASE_PRODUCT: string;
export declare const RELEASE_COMPONENT: string;
export declare const MANAGER_HASH_DOMAIN: string;
export declare const VERSION_RE: RegExp;
export declare const BUILD_ID_RE: RegExp;
export declare const PRODUCTION_TOP_LEVEL_FILES: string[];
export declare function adapterRoot(fromUrl?: string): string;
export declare function canonicalPyprojectPath(webRoot?: string): string;
export declare function parseProductVersionFromPyprojectText(
  text: string,
  sourceLabel?: string,
): string;
export declare function readProductVersion(webRoot?: string): string;
export declare function validateManagerRelease(value: unknown): {
  contract: number;
  product: string;
  product_version: string;
  component: string;
  component_version: string;
  build_id: string;
};
export declare function collectManagerInputFiles(webRoot?: string): string[];
export declare function readManagerInputEntries(webRoot?: string): Array<{
  rel: string;
  data: Buffer;
}>;
export declare function computeManagerBuildId(
  productVersion: string,
  entries: Array<{ rel: string; data: Buffer | Uint8Array }>,
): string;
export declare function computeManagerRelease(webRoot?: string): {
  contract: number;
  product: string;
  product_version: string;
  component: string;
  component_version: string;
  build_id: string;
};
