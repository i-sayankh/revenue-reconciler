import Link from "next/link";

import { Button } from "@/components/ui/button";

export default function Home() {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-4">
      <p className="text-muted-foreground">Revenue Reconciler</p>
      <div className="flex gap-2">
        <Button variant="outline" render={<Link href="/login" />}>
          Sign in
        </Button>
        <Button render={<Link href="/signup" />}>Create account</Button>
      </div>
    </div>
  );
}
