import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router";
import { DataProvider } from "@/lib/data";
import Landing from "@/landing/Landing";
import Login from "@/pages/Login";
import AppShell from "@/shell/AppShell";

const Dashboard = lazy(() => import("@/pages/Dashboard"));
const LiveCheck = lazy(() => import("@/pages/LiveCheck"));
const Events = lazy(() => import("@/pages/Events"));
const Approvals = lazy(() => import("@/pages/Approvals"));
const Users = lazy(() => import("@/pages/Users"));
const Simulator = lazy(() => import("@/pages/Simulator"));
const Runs = lazy(() => import("@/pages/Runs"));
const Benchmark = lazy(() => import("@/pages/Benchmark"));
const AttackLab = lazy(() => import("@/pages/AttackLab"));
const Audit = lazy(() => import("@/pages/Audit"));
const named = (name: "Architecture" | "Guardrails" | "DataLineage" | "About") =>
  lazy(() => import("@/pages/Reference").then((m) => ({ default: m[name] })));
const Architecture = named("Architecture");
const Guardrails = named("Guardrails");
const DataLineage = named("DataLineage");
const About = named("About");

export default function App() {
  return (
    <Suspense fallback={null}>
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="/login" element={<Login />} />
        <Route path="/app" element={<DataProvider><AppShell /></DataProvider>}>
          <Route index element={<Dashboard />} />
          <Route path="live/:source?" element={<LiveCheck />} />
          <Route path="events" element={<Events />} />
          <Route path="approvals" element={<Approvals />} />
          <Route path="runs" element={<Runs />} />
          <Route path="simulator" element={<Simulator />} />
          <Route path="users" element={<Users />} />
          <Route path="benchmark" element={<Benchmark />} />
          <Route path="attack-lab/:id?" element={<AttackLab />} />
          <Route path="audit" element={<Audit />} />
          <Route path="architecture" element={<Architecture />} />
          <Route path="guardrails" element={<Guardrails />} />
          <Route path="data" element={<DataLineage />} />
          <Route path="about" element={<About />} />
          <Route path="*" element={<Navigate to="/app" replace />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Suspense>
  );
}
