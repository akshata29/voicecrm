/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Fluent 2 "brand" blue ramp
        brand: {
          50: '#eff6fc',
          100: '#deecf9',
          200: '#c7e0f4',
          300: '#71afe5',
          400: '#2b88d8',
          500: '#0f6cbd',
          600: '#115ea3',
          700: '#0f548c',
          800: '#0c3b5e',
          900: '#082338',
        },
        canvas: '#f5f5f5',
      },
      fontFamily: {
        sans: [
          '"Segoe UI Variable Text"',
          '"Segoe UI"',
          'system-ui',
          '-apple-system',
          'BlinkMacSystemFont',
          '"Helvetica Neue"',
          'Arial',
          'sans-serif',
        ],
        mono: ['"Cascadia Mono"', 'Consolas', '"SFMono-Regular"', 'Menlo', 'monospace'],
      },
      boxShadow: {
        card: '0 0 2px rgba(0, 0, 0, 0.08), 0 1px 2px rgba(0, 0, 0, 0.06)',
        pop: '0 8px 16px rgba(0, 0, 0, 0.14), 0 0 2px rgba(0, 0, 0, 0.12)',
      },
      keyframes: {
        'fade-in': { from: { opacity: '0', transform: 'translateY(4px)' }, to: { opacity: '1', transform: 'none' } },
        'slide-in-left': { from: { transform: 'translateX(-100%)' }, to: { transform: 'none' } },
        'pulse-soft': { '0%, 100%': { opacity: '1' }, '50%': { opacity: '0.55' } },
        shimmer: { '100%': { transform: 'translateX(100%)' } },
        'record-ring': {
          '0%': { boxShadow: '0 0 0 0 rgba(220, 38, 38, 0.45)' },
          '100%': { boxShadow: '0 0 0 22px rgba(220, 38, 38, 0)' },
        },
      },
      animation: {
        'fade-in': 'fade-in 0.18s ease-out',
        'slide-in-left': 'slide-in-left 0.2s ease-out',
        'pulse-soft': 'pulse-soft 1.6s ease-in-out infinite',
        shimmer: 'shimmer 1.4s infinite',
        'record-ring': 'record-ring 1.4s ease-out infinite',
      },
    },
  },
  plugins: [],
};
