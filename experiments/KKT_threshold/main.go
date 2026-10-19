package main
import("encoding/json";"os";"fmt")
type Case struct {Name string;Y []int64;Mu []int64}
type Input struct {Mask MaskedSubproblem;State ADMMState;Park int;Cases []Case}
func main(){var in Input;if e:=json.NewDecoder(os.Stdin).Decode(&in);e!=nil{panic(e)};out:=[]map[string]interface{}{};for _,c:=range in.Cases{r,e:=computeKKTOnChain(&in.Mask,&in.State,in.Park,c.Y,c.Mu);if e!=nil{panic(e)};var pv,dv int64;for i,row:=range in.Mask.GMaskedScaled{var gy int64;for j,v:=range row{gy+=divRound(v*c.Y[j],MaskScale)};if gy-in.Mask.HVecScaled[i]>pv{pv=gy-in.Mask.HVecScaled[i]};if -c.Mu[i]>dv{dv=-c.Mu[i]}};if r.Pass!=(pv<=10000&&dv<=10000&&r.StatRes<=10000&&r.ComplRes<=10000&&!r.Saturated){panic(fmt.Sprint("diagnostic mismatch ",c.Name))};out=append(out,map[string]interface{}{"name":c.Name,"stationarity":r.StatRes,"complementarity":r.ComplRes,"primal_violation":pv,"dual_violation":dv,"pass":r.Pass,"saturated":r.Saturated})};json.NewEncoder(os.Stdout).Encode(out)}
