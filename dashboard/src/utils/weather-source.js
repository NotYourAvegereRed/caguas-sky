// Stub for the sky-only build: no network, fixed location.
// getWeatherData rejects on purpose so background.js keeps the "clear" scene
// and uses its own local sun-calc for sunrise/sunset.
export async function getWeatherData() { throw new Error("offline sky build"); }
export function detectLocation() { return Promise.reject(new Error("fixed location")); }
