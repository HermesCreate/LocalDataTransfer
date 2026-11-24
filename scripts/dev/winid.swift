// Dev helper for screenshots: print "<windowID> x y w h" for on-screen windows whose
// title contains argv[1] (owner filter optional argv[2]).
import CoreGraphics
import Foundation
let needle = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : ""
let owner = CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : ""
let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as! [[String: Any]]
for w in list {
  let name = w[kCGWindowName as String] as? String ?? ""
  let own = w[kCGWindowOwnerName as String] as? String ?? ""
  let layer = w[kCGWindowLayer as String] as? Int ?? 0
  if layer > 25 { continue }
  if !needle.isEmpty && !name.contains(needle) { continue }
  if !owner.isEmpty && !own.contains(owner) { continue }
  let b = w[kCGWindowBounds as String] as! [String: Any]
  print("\(w[kCGWindowNumber as String]!) \(b["X"]!) \(b["Y"]!) \(b["Width"]!) \(b["Height"]!) \(own) | \(name)")
}
