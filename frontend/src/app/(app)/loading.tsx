import { PageSkeleton } from "@/components/ui/PageSkeleton";

/*
 * Shown inside the shell the moment a link to another module is followed, instead of the old
 * page staying put until the server answers. Every page is rendered per request (the CSP
 * nonce), so without this a click gave no feedback until the response arrived (seconds while
 * `next dev` compiles a route), and production could not prefetch any part of the route.
 */
export default function Loading() {
  return <PageSkeleton />;
}
