# sky130-verify: .mag to .gds conversion for the KLayout cross-check
# (--with-klayout); KLayout does not read .mag.
#
# Input: the view copied to work_dir/<cell>.mag.
# Output: converted.gds in the working directory.

set cell $env(SKY130VERIFY_CELL)

if {[catch {
    load $cell
} err]} {
    puts "GDSWRITE_LOAD_FAIL $err"
    quit -noprompt
}
puts "GDSWRITE_LOAD_OK"

if {[catch {
    gds write converted.gds
} err]} {
    puts "GDSWRITE_FAIL $err"
    quit -noprompt
}
puts "GDSWRITE_OK"
quit -noprompt
