/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        page: "#FAFAF7",
        card: "#FFFFFF",
        accent: "#2563EB",
        tool: "#F5F5F4",
      },
      borderRadius: {
        none: "0",
      },
      fontFamily: {
        sans: ["ui-sans-serif", "system-ui", "sans-serif"],
      },
    },
  },
  plugins: [],
};
