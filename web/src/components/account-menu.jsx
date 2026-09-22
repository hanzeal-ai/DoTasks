import { Button } from "./ui/button";
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator } from "./ui/dropdown-menu";

export function AccountMenu({ username, authenticationEnabled }) {
  const select = action => document.dispatchEvent(new CustomEvent("account-action", { detail: action }));
  return <DropdownMenu>
    <DropdownMenuTrigger asChild>
      <Button variant="ghost" className="account-trigger" aria-label={`个人中心：${username}`}>
        <span className="account-avatar" aria-hidden="true">{username.slice(0, 1).toUpperCase()}</span>
        <span className="account-name">{username}</span><span aria-hidden="true">⌃</span>
      </Button>
    </DropdownMenuTrigger>
    <DropdownMenuContent side="top" align="start" className="account-menu">
      <DropdownMenuItem onSelect={() => select("settings")}>设置</DropdownMenuItem>
      {authenticationEnabled && <><DropdownMenuSeparator /><DropdownMenuItem onSelect={() => select("logout")}>退出登录</DropdownMenuItem></>}
    </DropdownMenuContent>
  </DropdownMenu>;
}
