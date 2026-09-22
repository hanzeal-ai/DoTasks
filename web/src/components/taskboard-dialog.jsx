import { useLayoutEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { Dialog as DialogRoot, DialogContent } from "./ui/dialog";
import { releaseContent } from "../ui-markup";

const dialogs = new Map();
export function isDialogOpen(id) { return Boolean(document.getElementById(id)); }
export function openDialog(id) { flushSync(() => dialogs.get(id)?.(true)); }
export function closeDialog(id) { flushSync(() => dialogs.get(id)?.(false)); }

export function Dialog({ id, className, children }) {
  const [open, setOpen] = useState(false);
  const returnFocus = useRef(null);
  const changeOpen = next => {
    if (next) returnFocus.current = document.activeElement;
    else releaseContent(document.getElementById(id));
    setOpen(next);
  };
  useLayoutEffect(() => {
    dialogs.set(id, changeOpen);
    return () => { dialogs.delete(id); releaseContent(document.getElementById(id)); };
  }, [id]);
  return <DialogRoot open={open} onOpenChange={changeOpen}>
    <DialogContent id={id} className={className}
      onCloseAutoFocus={event => { event.preventDefault(); if (returnFocus.current?.isConnected) returnFocus.current.focus(); }}>
      {children}
    </DialogContent>
  </DialogRoot>;
}
