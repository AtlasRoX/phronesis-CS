# Phronesis — Web Application

> **Phronesis** — Clinical and Translational R&D Web Interface  
> **Made by ChronoStrider Engineering (ChronoStrider Enginnerings)**

This is the Next.js frontend application for **Phronesis**, the biomedical research assistant.

## Getting Started

### 1. Prerequisites
- Node.js 20+ (Node 22+ recommended)
- Running Phronesis FastAPI backend (default: `http://127.0.0.1:8000`)

### 2. Run the Development Server

```bash
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000) with your browser to access the Phronesis research interface.

### 3. Build & Production

```bash
npm run typecheck
npm run test
npm run build
npm start
```

## Stack

- **Framework:** Next.js (App Router)
- **Styling:** Tailwind CSS
- **Icons:** Lucide React
- **Backend API:** Connects to Phronesis FastAPI via `NEXT_PUBLIC_API_URL` (default `http://127.0.0.1:8000`)

---
**Phronesis** — Engineered by **ChronoStrider Engineering** (**ChronoStrider Enginnerings**).
