import ChannelPage from "./ChannelPage";
import { fetchChannel } from "../../lib/api";

export default async function Page({ params }: { params: Promise<{ handle: string }> }) {
  const { handle } = await params;
  const data = await fetchChannel(handle);
  return <ChannelPage data={data} />;
}
