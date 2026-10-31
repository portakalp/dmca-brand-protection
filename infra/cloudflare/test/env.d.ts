// Vite's ?raw import (test/config.test.ts reads wrangler.jsonc as text).
declare module "*?raw" {
  const content: string;
  export default content;
}
