// Trusted bootstrap issues one receive capability and attenuated send rights.
import { Task, taskGet } from "../task/task.m"
import { endpointBootstrapService, handleCopy, handleClose } from "../ipc/objects.m"
import { RIGHT_RECEIVE, RIGHT_SEND } from "../arch/wrm081632/defs.m"

let serviceEndpoint(receiver: UWord, sender: UWord, receive: *mut UWord, send: *mut UWord): Bool {
    let server: *mut Task = taskGet(receiver)
    let client: *mut Task = taskGet(sender)
    let root: Word = endpointBootstrapService(&mut server.handles, receiver)
    if root < 0 return false
    let rx: Word = handleCopy(&mut server.handles, root as UWord,
        &mut server.handles, receiver, receiver, RIGHT_RECEIVE)
    if rx < 0 return false
    let tx: Word = handleCopy(&mut server.handles, root as UWord,
        &mut client.handles, receiver, sender, RIGHT_SEND)
    if tx < 0 return false
    if handleClose(&mut server.handles, root as UWord) != 0 return false
    receive[0] = rx as UWord
    send[0] = tx as UWord
    return true
}

export { serviceEndpoint }
