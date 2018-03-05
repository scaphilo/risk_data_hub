const React = require('react');
const {BarChart, Bar, XAxis, Cell, YAxis, Tooltip, CartesianGrid, ResponsiveContainer} = require('recharts');
const ChartTooltip = require("./ChartTooltip");



const EventCountryChart = React.createClass({
    propTypes: {
        values: React.PropTypes.array,
        getAnalysisData: React.PropTypes.func,
        full_context: React.PropTypes.object      
    },
    getDefaultProps() {
        return {
        };
    },
    getChartData() {
        const {values} = this.props;
//console.log("enter get chart method");
        //console.log(values);
        //return values.filter((d) => d[dim.dim1] === val ).map((v) => {return {"name": v[dim.dim2], "value": parseFloat(v[2], 10)}; });
        return values.map((v) => {return {"name": v[0], "value": parseFloat(v[3], 10)}; });        
    },
    render() {  
        //console.log("render called");
        const chartData = this.getChartData();
        //console.log(chartData !== null);        
        const dimensionX = 'Country';
        /*const colors = chromaJs.scale('OrRd').colors(chartData.length);*/
        return (          
            <ResponsiveContainer width="100%" height={200}>
                <BarChart width={500} height={200} data={chartData} margin={{top: 20, right: 0, left: 0, bottom: 5}}>
                    <CartesianGrid strokeDasharray="3 3" />
                    <XAxis dataKey="name"/>
                    <YAxis/> 
                    <Tooltip/>                
                    <Bar dataKey="value">                    
                        {chartData.map((entry,index) => {
                            const ctx = this.props.full_context;
                            const active = entry.name === ctx.loc;
                            return(
                                <Cell cursor="pointer" stroke={"#ff8f31"} strokeWidth={active ? 2 : 0}fill={active ? '#ff8f31' : '#2c689c'} key={`cell-${index}`}/>);                            
                        })}
                    </Bar>
                </BarChart>
            </ResponsiveContainer>);
    },
    handleClick(item, index) {
        const ctx = this.props.full_context;        
        //console.log(item);
        this.props.getAnalysisData('/risks/data_extraction/loc/' + item.name + '/ht/' + ctx.ht + '/at/' + ctx.at + '/an/' + ctx.an + '/');
    },
    formatYTiks(v) {
        return v.toLocaleString();
    },
    formatXTiks(v) {
        return !isNaN(v) && parseFloat(v).toLocaleString() || v;
    }
});

module.exports = EventCountryChart;
