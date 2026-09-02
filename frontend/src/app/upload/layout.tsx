import { AuthGuard } from "@/components/auth-guard";

export default function UploadLayout({ children }: LayoutProps<"/upload">) {
  return <AuthGuard>{children}</AuthGuard>;
}
